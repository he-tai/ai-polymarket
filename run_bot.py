#!/usr/bin/env python3
"""Polymarket 量化脚手架：研究、回测与实盘执行。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

from polymarket.auto_trader import AutoTradeConfig, auto_trade_markets
from polymarket.backtest import BacktestCostModel, run_mean_reversion_backtest
from polymarket.clob_public import ClobPublicClient
from polymarket.config import (
    LiveTradingConfig,
    database_url_from_env,
    deepseek_config_from_env,
    live_trading_config_from_env,
)
from polymarket.deepseek_analysis import AnalysisInput, build_prompt, request_analysis
from polymarket.execution import OrderIntent, build_trading_client, submit_limit
from polymarket.gamma import GammaClient
from polymarket.live_runner import LiveLoopConfig, run_live_loop
from polymarket.logging_utils import init_trade_loggers, log_json
from polymarket.market_utils import outcome_legs, pretty_market_header
from polymarket.multi_live_runner import KillSwitchConfig, MarketSelection, MultiLiveConfig, run_multi_market_live
from polymarket.research import load_price_series
from polymarket.reporting import generate_attribution_report
from polymarket.risk import RiskLimits
from polymarket.storage import TradingStorage
from polymarket.strategy import MeanReversionConfig

LOGGERS = init_trade_loggers("logs")


def _pick_market(gamma: GammaClient, slug: str | None) -> dict:
    if slug:
        return gamma.get_market_by_slug(slug)
    markets = gamma.list_markets(limit=1, offset=0, order="volume24hr", ascending=False)
    if not markets:
        raise RuntimeError("No markets returned from Gamma")
    return markets[0]


def _add_common_market_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--slug", default=None, help="Market slug from the /event/... URL path segment")
    p.add_argument("--outcome-index", type=int, default=0, help="Index into outcomes/clobTokenIds")


def _resolve_leg(args, gamma: GammaClient):
    market = _pick_market(gamma, args.slug)
    print(pretty_market_header(market))
    legs = outcome_legs(market)
    if args.outcome_index < 0 or args.outcome_index >= len(legs):
        raise SystemExit(f"--outcome-index must be in [0,{len(legs) - 1}]")
    leg = legs[args.outcome_index]
    print(f"Outcome[{args.outcome_index}] {leg.label!r} token_id={leg.token_id}")
    return market, leg


def _backtest_cost_from_args(args) -> BacktestCostModel:
    return BacktestCostModel(
        slippage_bps=args.slippage_bps,
        taker_fee_bps=args.taker_fee_bps,
        maker_fee_bps=args.maker_fee_bps,
        impact_bps_per_unit=args.impact_bps_per_unit,
        use_taker=not args.assume_maker,
    )


def _risk_limits_from_args(args) -> RiskLimits:
    return RiskLimits(
        max_notional_per_trade=args.max_notional_per_trade,
        max_position_size=args.max_position_size,
        max_daily_loss=args.max_daily_loss,
        min_edge_bps=args.min_edge_bps,
    )


def _cmd_snapshot(args) -> int:
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        _, leg = _resolve_leg(args, gamma)
        tob = clob.top_of_book(leg.token_id)
        print(
            "TopOfBook:",
            f"bid={tob.best_bid} ask={tob.best_ask} tick={tob.tick_size} min_size={tob.min_order_size} neg_risk={tob.neg_risk}",
        )
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_research(args) -> int:
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        _, leg = _resolve_leg(args, gamma)
        points = load_price_series(clob, leg.token_id, interval=args.interval, fidelity=args.fidelity)
        if not points:
            print("No historical points returned.")
            return 0
        prices = [p.price for p in points]
        print(f"history_points={len(points)} first_ts={points[0].ts} last_ts={points[-1].ts}")
        print(f"price_min={min(prices):.4f} price_max={max(prices):.4f} last={prices[-1]:.4f}")
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_backtest(args) -> int:
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        _, leg = _resolve_leg(args, gamma)
        points = load_price_series(clob, leg.token_id, interval=args.interval, fidelity=args.fidelity)
        prices = [p.price for p in points]
        res = run_mean_reversion_backtest(
            prices,
            cfg=MeanReversionConfig(window=args.window, z_entry=args.z_entry),
            limits=_risk_limits_from_args(args),
            order_size=args.size,
            cost=_backtest_cost_from_args(args),
        )
        win_rate = (res.wins / max(1, res.wins + res.losses)) * 100.0
        print(
            f"backtest trades={res.trades} wins={res.wins} losses={res.losses} "
            f"win_rate={win_rate:.2f}% gross_pnl={res.gross_pnl:.4f} final_position={res.final_position:.2f}"
        )
        print("Warning: this is a simplistic backtest baseline, not production-grade alpha validation.")
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_ai_report(args) -> int:
    deepseek_cfg = deepseek_config_from_env()
    if deepseek_cfg is None:
        raise SystemExit("Missing DEEPSEEK_API_KEY in environment (.env).")
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        market, leg = _resolve_leg(args, gamma)
        points = load_price_series(clob, leg.token_id, interval=args.interval, fidelity=args.fidelity)
        prices = [p.price for p in points]
        if not prices:
            raise SystemExit("No historical points returned from prices-history.")
        bt = run_mean_reversion_backtest(
            prices,
            cfg=MeanReversionConfig(window=args.window, z_entry=args.z_entry),
            limits=_risk_limits_from_args(args),
            order_size=args.size,
            cost=_backtest_cost_from_args(args),
        )
        wins_total = max(1, bt.wins + bt.losses)
        win_rate = (bt.wins / wins_total) * 100.0
        tob = clob.top_of_book(leg.token_id)
        spread = float(tob.best_ask - tob.best_bid) if tob.best_bid is not None and tob.best_ask is not None else None

        payload = AnalysisInput(
            market_question=str(market.get("question", "")),
            market_slug=str(market.get("slug", "")),
            outcome_label=leg.label,
            history_points=len(prices),
            price_min=min(prices),
            price_max=max(prices),
            last_price=prices[-1],
            spread=spread,
            backtest_trades=bt.trades,
            backtest_win_rate=win_rate,
            backtest_gross_pnl=bt.gross_pnl,
            backtest_final_position=bt.final_position,
        )
        report = request_analysis(deepseek_cfg, build_prompt(payload))
        print("\n===== DeepSeek Analysis Report =====\n")
        print(report)
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_limit_order(args) -> int:
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        market, leg = _resolve_leg(args, gamma)
        tob = clob.top_of_book(leg.token_id)
        print(
            "TopOfBook:",
            f"bid={tob.best_bid} ask={tob.best_ask} tick={tob.tick_size} min_size={tob.min_order_size} neg_risk={tob.neg_risk}",
        )
        if args.size < float(tob.min_order_size):
            raise SystemExit(f"--size must be >= min_order_size ({tob.min_order_size})")
        cfg = live_trading_config_from_env()
        if cfg is None:
            raise SystemExit("Missing PRIVATE_KEY or FUNDER_ADDRESS in environment (.env).")
        effective_cfg = LiveTradingConfig(
            private_key=cfg.private_key,
            funder_address=cfg.funder_address,
            signature_type=(cfg.signature_type if args.signature_type is None else int(args.signature_type)),
        )
        if args.confirm_live != "YES":
            raise SystemExit("Refusing live order. Pass --confirm-live YES to proceed.")
        intent = OrderIntent(
            token_id=leg.token_id,
            side=args.side,
            price=float(args.price),
            size=float(args.size),
            tick_size=tob.tick_size,
            neg_risk=tob.neg_risk,
        )
        client = build_trading_client(effective_cfg)
        resp = submit_limit(client, intent)
        log_json(
            LOGGERS["orders"],
            {
                "event": "manual_limit_order",
                "slug": str(market.get("slug", "")),
                "token_id": leg.token_id,
                "side": args.side,
                "price": float(args.price),
                "size": float(args.size),
                "signature_type": effective_cfg.signature_type,
                "response": resp,
            },
        )
        TradingStorage(database_url_from_env()).log_trade(
            {
                "market_slug": str(market.get("slug", "")),
                "token_id": leg.token_id,
                "side": args.side,
                "price": float(args.price),
                "size": float(args.size),
                "notional": float(args.price) * float(args.size),
                "fees": 0.0,
                "impact_cost": 0.0,
                "status": "submitted",
                "order_id": str(resp.get("orderID", "")) if isinstance(resp, dict) else "",
                "metadata": {"response": resp},
            }
        )
        print("Submitted:")
        print(json.dumps(resp, default=str, indent=2))
        return 0
    finally:
        gamma.close()
        clob.close()


def _select_multi_markets(args, gamma: GammaClient, clob: ClobPublicClient) -> list[MarketSelection]:
    slug_list = [s.strip() for s in args.slugs.split(",") if s.strip()] if args.slugs else []
    markets = [gamma.get_market_by_slug(s) for s in slug_list] if slug_list else gamma.list_markets(
        limit=args.top_markets,
        offset=0,
        order="volume24hr",
        ascending=False,
    )
    selected: list[MarketSelection] = []
    for m in markets:
        legs = outcome_legs(m)
        if args.outcome_index < 0 or args.outcome_index >= len(legs):
            continue
        leg = legs[args.outcome_index]
        tob = clob.top_of_book(leg.token_id)
        selected.append(
            MarketSelection(
                slug=str(m.get("slug", "")),
                question=str(m.get("question", "")),
                outcome_label=leg.label,
                token_id=leg.token_id,
                tick_size=float(tob.tick_size),
                neg_risk=tob.neg_risk,
            )
        )
    return selected


def _cmd_live_multi(args) -> int:
    if args.confirm_live != "YES":
        raise SystemExit("Refusing multi live loop. Pass --confirm-live YES to proceed.")
    cfg = live_trading_config_from_env()
    if cfg is None:
        raise SystemExit("Missing PRIVATE_KEY or FUNDER_ADDRESS in environment (.env).")
    effective_cfg = LiveTradingConfig(
        private_key=cfg.private_key,
        funder_address=cfg.funder_address,
        signature_type=(cfg.signature_type if args.signature_type is None else int(args.signature_type)),
    )
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        log_json(
            LOGGERS["runtime"],
            {
                "event": "live_multi_start",
                "top_markets": args.top_markets,
                "signature_type": effective_cfg.signature_type,
                "max_events": args.max_events,
            },
        )
        selected = _select_multi_markets(args, gamma, clob)
        if not selected:
            raise SystemExit("No markets selected for multi-live.")
        print(f"Selected {len(selected)} markets")
        for s in selected:
            print(f"- {s.slug} [{s.outcome_label}] {s.token_id}")

        client = build_trading_client(effective_cfg)
        storage = TradingStorage(database_url_from_env())
        asyncio.run(
            run_multi_market_live(
                markets=selected,
                trading_client=client,
                strategy_cfg=MeanReversionConfig(window=args.window, z_entry=args.z_entry),
                risk_limits=_risk_limits_from_args(args),
                kill_cfg=KillSwitchConfig(
                    max_consecutive_losses=args.max_consecutive_losses,
                    max_api_errors=args.max_api_errors,
                    max_abnormal_spread=args.max_abnormal_spread,
                    max_stale_seconds=args.max_stale_seconds,
                    max_portfolio_loss=args.max_portfolio_loss,
                    max_portfolio_notional=args.max_portfolio_notional,
                ),
                live_cfg=MultiLiveConfig(
                    order_size=args.size,
                    max_events=args.max_events,
                    heartbeat_seconds=args.heartbeat_seconds,
                    fee_bps=args.live_fee_bps,
                    reconcile_every_events=args.reconcile_every_events,
                ),
                storage=storage,
            )
        )
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_live_loop(args) -> int:
    if args.confirm_live != "YES":
        raise SystemExit("Refusing live loop. Pass --confirm-live YES to proceed.")
    cfg = live_trading_config_from_env()
    if cfg is None:
        raise SystemExit("Missing PRIVATE_KEY or FUNDER_ADDRESS in environment (.env).")
    effective_cfg = LiveTradingConfig(
        private_key=cfg.private_key,
        funder_address=cfg.funder_address,
        signature_type=(cfg.signature_type if args.signature_type is None else int(args.signature_type)),
    )
    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        log_json(
            LOGGERS["runtime"],
            {
                "event": "live_loop_start",
                "slug": args.slug,
                "signature_type": effective_cfg.signature_type,
                "max_loops": args.max_loops,
            },
        )
        _, leg = _resolve_leg(args, gamma)
        trading_client = build_trading_client(effective_cfg)
        run_live_loop(
            clob_public=clob,
            trading_client=trading_client,
            token_id=leg.token_id,
            strategy_cfg=MeanReversionConfig(window=args.window, z_entry=args.z_entry),
            risk_limits=_risk_limits_from_args(args),
            live_cfg=LiveLoopConfig(
                order_size=args.size,
                loop_seconds=args.loop_seconds,
                max_loops=args.max_loops,
                heartbeat_every_loops=args.heartbeat_every_loops,
            ),
        )
        return 0
    finally:
        gamma.close()
        clob.close()


def _cmd_attribution_report(args) -> int:
    storage = TradingStorage(database_url_from_env())
    paths = generate_attribution_report(storage, days=args.days, out_dir=args.out_dir)
    print("Attribution report generated:")
    print(f"- markdown: {paths.markdown_path}")
    print(f"- trades csv: {paths.trades_csv_path}")
    print(f"- pnl csv: {paths.pnl_csv_path}")
    return 0


def _cmd_preflight_live(args) -> int:
    cfg = live_trading_config_from_env()
    if cfg is None:
        raise SystemExit("缺少实盘环境变量：请在 .env 中填写 PRIVATE_KEY 与 FUNDER_ADDRESS。")

    # 允许在命令行临时覆盖 signatureType（便于社交登录用户在 1/2 之间切换验证）
    signature_type = cfg.signature_type if args.signature_type is None else int(args.signature_type)
    print("开始实盘预检（不会下单）")
    print(f"- SIGNATURE_TYPE={signature_type}")
    print(f"- FUNDER_ADDRESS={cfg.funder_address}")

    try:
        client = build_trading_client(
            type(cfg)(
                private_key=cfg.private_key,
                funder_address=cfg.funder_address,
                signature_type=signature_type,
            )
        )
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"初始化交易客户端失败：{exc}") from exc

    try:
        hb = client.post_heartbeat(None)
        print(f"心跳成功：{hb}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            "心跳失败（认证/签名可能不正确）。\n"
            f"错误：{exc}\n"
            "建议：如果你是社交账号登录，先尝试 SIGNATURE_TYPE=1；若仍失败再试 2。"
        ) from exc

    try:
        # 读取一次订单列表用于验证 L2 header 与权限（不产生交易）
        orders = client.get_orders()
        if isinstance(orders, dict) and "data" in orders:
            count = len(orders.get("data") or [])
        elif isinstance(orders, list):
            count = len(orders)
        else:
            count = 0
        print(f"读取订单列表成功：count≈{count}")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"读取订单列表失败：{exc}") from exc

    print("预检通过：你可以开始用 limit-order/live-loop/live-multi 做小额实盘测试。")
    return 0


def _cmd_funding_check(args) -> int:
    cfg = live_trading_config_from_env()
    if cfg is None:
        raise SystemExit("缺少实盘环境变量：请在 .env 中填写 PRIVATE_KEY 与 FUNDER_ADDRESS。")
    signature_type = cfg.signature_type if args.signature_type is None else int(args.signature_type)
    effective_cfg = LiveTradingConfig(
        private_key=cfg.private_key,
        funder_address=cfg.funder_address,
        signature_type=signature_type,
    )
    client = build_trading_client(effective_cfg)
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=signature_type)
    payload = client.get_balance_allowance(params)
    balance = float(payload.get("balance", "0") or 0)
    allowances = payload.get("allowances", {}) or {}
    max_allowance = 0.0
    for value in allowances.values():
        try:
            max_allowance = max(max_allowance, float(value))
        except (TypeError, ValueError):
            continue

    required_notional = float(args.price) * float(args.size)
    print("资金检查结果：")
    print(f"- SIGNATURE_TYPE={signature_type}")
    print(f"- FUNDER_ADDRESS={cfg.funder_address}")
    print(f"- balance={balance}")
    print(f"- max_allowance={max_allowance}")
    print(f"- required_notional(price*size)={required_notional}")
    enough_balance = balance >= required_notional
    enough_allowance = max_allowance >= required_notional
    if enough_balance and enough_allowance:
        print("结论：资金与授权均满足，当前可下单。")
        return 0
    print("结论：暂不可下单。")
    if not enough_balance:
        print("- 原因：余额不足。")
    if not enough_allowance:
        print("- 原因：授权额度不足。")
    print("建议：在前端完成充值/授权后重试 funding-check。")
    return 1


def _cmd_auto_trade(args) -> int:
    deepseek_cfg = deepseek_config_from_env()
    if deepseek_cfg is None:
        log_json(LOGGERS["runtime"], {"event": "auto_trade_error", "error": "missing_deepseek_api_key"})
        raise SystemExit("缺少 DEEPSEEK_API_KEY，无法进行 AI 自动分析下单。")

    trading_client = None
    if args.live:
        if args.confirm_live != "YES":
            log_json(LOGGERS["runtime"], {"event": "auto_trade_error", "error": "missing_confirm_live_yes"})
            raise SystemExit("自动下单需要 --live --confirm-live YES。")
        cfg = live_trading_config_from_env()
        if cfg is None:
            log_json(LOGGERS["runtime"], {"event": "auto_trade_error", "error": "missing_live_env"})
            raise SystemExit("缺少实盘环境变量：PRIVATE_KEY / FUNDER_ADDRESS。")
        effective_cfg = LiveTradingConfig(
            private_key=cfg.private_key,
            funder_address=cfg.funder_address,
            signature_type=(cfg.signature_type if args.signature_type is None else int(args.signature_type)),
        )
        trading_client = build_trading_client(effective_cfg)
    log_json(
        LOGGERS["runtime"],
        {
            "event": "auto_trade_start",
            "live": args.live,
            "top_markets": args.top_markets,
            "max_orders": args.max_orders,
            "min_confidence": args.min_confidence,
            "default_size": args.default_size,
            "analysis_timeout_s": args.analysis_timeout_s,
        },
    )

    gamma = GammaClient()
    clob = ClobPublicClient()
    try:
        try:
            results = auto_trade_markets(
                gamma=gamma,
                clob=clob,
                trading_client=trading_client,
                deepseek_cfg=deepseek_cfg,
                cfg=AutoTradeConfig(
                    top_markets=args.top_markets,
                    max_orders=args.max_orders,
                    min_confidence=args.min_confidence,
                    default_size=args.default_size,
                    outcome_index=args.outcome_index,
                    live=args.live,
                ),
                analysis_timeout_s=args.analysis_timeout_s,
                loggers=LOGGERS,
            )
        except Exception as exc:  # noqa: BLE001
            log_json(LOGGERS["runtime"], {"event": "auto_trade_exception", "error": str(exc)})
            raise
        log_json(
            LOGGERS["runtime"],
            {
                "event": "auto_trade_run",
                "live": args.live,
                "top_markets": args.top_markets,
                "max_orders": args.max_orders,
                "min_confidence": args.min_confidence,
                "result_count": len(results),
            },
        )
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        submitted = sum(1 for x in results if x.get("status") == "submitted")
        planned = sum(1 for x in results if x.get("status") == "planned")
        print(f"自动交易完成：submitted={submitted}, planned={planned}, total={len(results)}")
        return 0
    finally:
        gamma.close()
        clob.close()


def _add_cost_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--slippage-bps", type=float, default=5.0)
    p.add_argument("--taker-fee-bps", type=float, default=7.0)
    p.add_argument("--maker-fee-bps", type=float, default=0.0)
    p.add_argument("--impact-bps-per-unit", type=float, default=0.2)
    p.add_argument("--assume-maker", action="store_true")


def _add_risk_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--max-notional-per-trade", type=float, default=25.0)
    p.add_argument("--max-position-size", type=float, default=200.0)
    p.add_argument("--max-daily-loss", type=float, default=50.0)
    p.add_argument("--min-edge-bps", type=float, default=10.0)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Polymarket research/backtest/live scaffold")
    sp = p.add_subparsers(dest="command", required=True)

    snapshot = sp.add_parser("snapshot", help="Print market + top of book")
    _add_common_market_args(snapshot)
    snapshot.set_defaults(func=_cmd_snapshot)

    research = sp.add_parser("research", help="Pull historical prices summary")
    _add_common_market_args(research)
    research.add_argument("--interval", default="1h", choices=["max", "all", "1m", "1w", "1d", "6h", "1h"])
    research.add_argument("--fidelity", type=int, default=5)
    research.set_defaults(func=_cmd_research)

    backtest = sp.add_parser("backtest", help="Run simple mean-reversion backtest")
    _add_common_market_args(backtest)
    backtest.add_argument("--interval", default="1h", choices=["max", "all", "1m", "1w", "1d", "6h", "1h"])
    backtest.add_argument("--fidelity", type=int, default=5)
    backtest.add_argument("--window", type=int, default=24)
    backtest.add_argument("--z-entry", type=float, default=1.2)
    backtest.add_argument("--size", type=float, default=5.0)
    _add_cost_args(backtest)
    _add_risk_args(backtest)
    backtest.set_defaults(func=_cmd_backtest)

    ai_report = sp.add_parser("ai-report", help="Call DeepSeek to analyze history + backtest + spread")
    _add_common_market_args(ai_report)
    ai_report.add_argument("--interval", default="1h", choices=["max", "all", "1m", "1w", "1d", "6h", "1h"])
    ai_report.add_argument("--fidelity", type=int, default=5)
    ai_report.add_argument("--window", type=int, default=24)
    ai_report.add_argument("--z-entry", type=float, default=1.2)
    ai_report.add_argument("--size", type=float, default=5.0)
    _add_cost_args(ai_report)
    _add_risk_args(ai_report)
    ai_report.set_defaults(func=_cmd_ai_report)

    limit_order = sp.add_parser("limit-order", help="Submit one live limit order")
    _add_common_market_args(limit_order)
    limit_order.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    limit_order.add_argument("--price", type=float, required=True, help="Limit price in [0,1]")
    limit_order.add_argument("--size", type=float, default=5.0, help="Order size (shares)")
    limit_order.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    limit_order.add_argument("--confirm-live", default="NO", help="Must be YES to allow live order submission")
    limit_order.set_defaults(func=_cmd_limit_order)

    live_loop = sp.add_parser("live-loop", help="Run polling live strategy loop with heartbeat")
    _add_common_market_args(live_loop)
    live_loop.add_argument("--window", type=int, default=24)
    live_loop.add_argument("--z-entry", type=float, default=1.2)
    live_loop.add_argument("--size", type=float, default=5.0)
    live_loop.add_argument("--loop-seconds", type=int, default=15)
    live_loop.add_argument("--max-loops", type=int, default=60)
    live_loop.add_argument("--heartbeat-every-loops", type=int, default=4)
    live_loop.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    _add_risk_args(live_loop)
    live_loop.add_argument("--confirm-live", default="NO", help="Must be YES to allow live execution")
    live_loop.set_defaults(func=_cmd_live_loop)

    live_multi = sp.add_parser("live-multi", help="WebSocket multi-market live strategy with kill switch + DB logs")
    live_multi.add_argument("--slugs", default="", help="Comma-separated slugs. If empty, uses top markets")
    live_multi.add_argument("--top-markets", type=int, default=3)
    live_multi.add_argument("--outcome-index", type=int, default=0)
    live_multi.add_argument("--window", type=int, default=24)
    live_multi.add_argument("--z-entry", type=float, default=1.2)
    live_multi.add_argument("--size", type=float, default=5.0)
    live_multi.add_argument("--max-events", type=int, default=1200)
    live_multi.add_argument("--heartbeat-seconds", type=int, default=20)
    live_multi.add_argument("--reconcile-every-events", type=int, default=20)
    live_multi.add_argument("--live-fee-bps", type=float, default=7.0)
    _add_risk_args(live_multi)
    live_multi.add_argument("--max-consecutive-losses", type=int, default=4)
    live_multi.add_argument("--max-api-errors", type=int, default=5)
    live_multi.add_argument("--max-abnormal-spread", type=float, default=0.15)
    live_multi.add_argument("--max-stale-seconds", type=int, default=30)
    live_multi.add_argument("--max-portfolio-loss", type=float, default=150.0)
    live_multi.add_argument("--max-portfolio-notional", type=float, default=500.0)
    live_multi.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    live_multi.add_argument("--confirm-live", default="NO", help="Must be YES to allow live execution")
    live_multi.set_defaults(func=_cmd_live_multi)

    attribution = sp.add_parser("attribution-report", help="Generate trading attribution markdown + csv exports")
    attribution.add_argument("--days", type=int, default=7)
    attribution.add_argument("--out-dir", default="reports")
    attribution.set_defaults(func=_cmd_attribution_report)

    preflight = sp.add_parser("preflight-live", help="实盘预检：验证认证/签名配置（不下单）")
    preflight.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    preflight.set_defaults(func=_cmd_preflight_live)

    funding = sp.add_parser("funding-check", help="检查余额与授权是否满足下单要求（不下单）")
    funding.add_argument("--price", type=float, required=True, help="计划下单价格")
    funding.add_argument("--size", type=float, required=True, help="计划下单数量")
    funding.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    funding.set_defaults(func=_cmd_funding_check)

    auto_trade = sp.add_parser("auto-trade", help="拉取市场并调用 DeepSeek 自动分析后下单")
    auto_trade.add_argument("--top-markets", type=int, default=10, help="参与分析的市场数量")
    auto_trade.add_argument("--max-orders", type=int, default=2, help="单次最多下单数量")
    auto_trade.add_argument("--min-confidence", type=float, default=0.7, help="最小置信度阈值")
    auto_trade.add_argument("--default-size", type=float, default=5.0, help="默认下单数量")
    auto_trade.add_argument("--outcome-index", type=int, default=0, help="默认 outcome 索引")
    auto_trade.add_argument("--analysis-timeout-s", type=float, default=45.0, help="DeepSeek 单次分析超时秒数")
    auto_trade.add_argument("--live", action="store_true", help="开启真实下单；默认仅输出计划")
    auto_trade.add_argument("--signature-type", type=int, default=None, help="可选：临时覆盖 SIGNATURE_TYPE")
    auto_trade.add_argument("--confirm-live", default="NO", help="自动下单时必须为 YES")
    auto_trade.set_defaults(func=_cmd_auto_trade)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

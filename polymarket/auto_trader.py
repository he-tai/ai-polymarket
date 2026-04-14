from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from polymarket.clob_public import ClobPublicClient
from polymarket.deepseek_analysis import request_analysis
from polymarket.execution import OrderIntent, submit_limit
from polymarket.gamma import GammaClient
from polymarket.logging_utils import log_json
from polymarket.market_utils import outcome_legs


@dataclass(frozen=True)
class AutoTradeConfig:
    top_markets: int = 10
    max_orders: int = 2
    min_confidence: float = 0.7
    default_size: float = 5.0
    outcome_index: int = 0
    live: bool = False


def _extract_json(text: str) -> dict[str, Any]:
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            s = s.split("\n", 1)[1]
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


def _decision_prompt(
    *,
    market: dict[str, Any],
    outcome_labels: list[str],
    outcome_prices: list[float],
    top_books: list[dict[str, Any]],
    default_size: float,
) -> str:
    return (
        "你是预测市场量化交易助手。请根据市场信息给出严格 JSON 决策，不要输出任何解释性文字。\n"
        "输出字段必须包含：\n"
        '{ "action":"BUY|SELL|SKIP", "outcome_index":0, "limit_price":0.5, "size":5, "confidence":0.0, "reason":"..." }\n'
        "规则：\n"
        "1) 若信号不明确，action=SKIP。\n"
        "2) confidence 取 [0,1]。\n"
        "3) limit_price 取 [0.01,0.99]。\n"
        "4) size 用数字。\n\n"
        f"问题: {market.get('question','')}\n"
        f"slug: {market.get('slug','')}\n"
        f"outcomes: {outcome_labels}\n"
        f"outcome_prices(前端): {outcome_prices}\n"
        f"orderbooks: {top_books}\n"
        f"default_size: {default_size}\n"
    )


def auto_trade_markets(
    *,
    gamma: GammaClient,
    clob: ClobPublicClient,
    trading_client,
    deepseek_cfg,
    cfg: AutoTradeConfig,
    analysis_timeout_s: float = 60.0,
    loggers: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    markets = gamma.list_markets(limit=cfg.top_markets, offset=0, order="volume24hr", ascending=False)
    actions: list[dict[str, Any]] = []
    live_orders = 0

    for market in markets:
        if live_orders >= cfg.max_orders:
            break
        try:
            legs = outcome_legs(market)
        except Exception:
            continue
        if not legs:
            continue
        if cfg.outcome_index >= len(legs):
            continue

        # 准备每个 outcome 的盘口摘要给模型
        outcome_labels = [x.label for x in legs]
        raw_prices = market.get("outcomePrices")
        try:
            outcome_prices = [float(x) for x in json.loads(raw_prices)] if isinstance(raw_prices, str) else []
        except Exception:
            outcome_prices = []

        top_books: list[dict[str, Any]] = []
        for i, leg in enumerate(legs[:2]):
            try:
                tob = clob.top_of_book(leg.token_id)
                top_books.append(
                    {
                        "outcome_index": i,
                        "label": leg.label,
                        "best_bid": float(tob.best_bid) if tob.best_bid is not None else None,
                        "best_ask": float(tob.best_ask) if tob.best_ask is not None else None,
                        "tick_size": float(tob.tick_size),
                        "min_order_size": float(tob.min_order_size),
                        "neg_risk": tob.neg_risk,
                    }
                )
            except Exception:
                continue
        if not top_books:
            continue

        prompt = _decision_prompt(
            market=market,
            outcome_labels=outcome_labels,
            outcome_prices=outcome_prices,
            top_books=top_books,
            default_size=cfg.default_size,
        )
        try:
            raw = request_analysis(deepseek_cfg, prompt, timeout_s=analysis_timeout_s)
        except Exception as exc:  # noqa: BLE001
            actions.append({"slug": market.get("slug"), "action": "SKIP", "reason": f"DeepSeek 请求失败: {exc}"})
            if loggers and loggers.get("analysis"):
                log_json(loggers["analysis"], {"slug": market.get("slug"), "event": "analysis_timeout", "error": str(exc)})
            continue
        try:
            decision = _extract_json(raw)
        except Exception as exc:
            actions.append({"slug": market.get("slug"), "action": "SKIP", "reason": f"模型输出不可解析: {exc}"})
            if loggers and loggers.get("analysis"):
                log_json(
                    loggers["analysis"],
                    {"slug": market.get("slug"), "event": "analysis_parse_error", "error": str(exc), "raw": raw},
                )
            continue

        action = str(decision.get("action", "SKIP")).upper()
        confidence = float(decision.get("confidence", 0.0) or 0.0)
        out_idx = int(decision.get("outcome_index", cfg.outcome_index))
        if action == "SKIP" or confidence < cfg.min_confidence or out_idx < 0 or out_idx >= len(legs):
            if loggers and loggers.get("analysis"):
                log_json(
                    loggers["analysis"],
                    {
                        "slug": market.get("slug"),
                        "event": "analysis_skip",
                        "action": action,
                        "confidence": confidence,
                        "reason": decision.get("reason", ""),
                    },
                )
            actions.append(
                {
                    "slug": market.get("slug"),
                    "action": "SKIP",
                    "confidence": confidence,
                    "reason": decision.get("reason", "低置信度或跳过"),
                }
            )
            continue

        leg = legs[out_idx]
        tob = clob.top_of_book(leg.token_id)
        if tob.best_bid is None or tob.best_ask is None:
            actions.append({"slug": market.get("slug"), "action": "SKIP", "reason": "无有效买卖盘"})
            continue

        side = "BUY" if action == "BUY" else "SELL"
        limit_price = float(decision.get("limit_price", 0.0) or 0.0)
        size = float(decision.get("size", cfg.default_size) or cfg.default_size)
        # 基本保护：价格约束到合理区间
        limit_price = max(0.01, min(0.99, limit_price))
        if size < float(tob.min_order_size):
            size = float(tob.min_order_size)

        plan = {
            "slug": market.get("slug"),
            "question": market.get("question"),
            "outcome_index": out_idx,
            "outcome_label": leg.label,
            "token_id": leg.token_id,
            "side": side,
            "price": limit_price,
            "size": size,
            "confidence": confidence,
            "reason": decision.get("reason", ""),
            "live": cfg.live,
        }

        if cfg.live:
            intent = OrderIntent(
                token_id=leg.token_id,
                side=side,  # type: ignore[arg-type]
                price=limit_price,
                size=size,
                tick_size=tob.tick_size,
                neg_risk=tob.neg_risk,
            )
            try:
                resp = submit_limit(trading_client, intent)
                plan["status"] = "submitted"
                plan["response"] = resp
                live_orders += 1
                if loggers and loggers.get("orders"):
                    log_json(loggers["orders"], {"event": "order_submitted", **plan})
            except Exception as exc:  # noqa: BLE001
                plan["status"] = "failed"
                plan["error"] = str(exc)
                if loggers and loggers.get("orders"):
                    log_json(loggers["orders"], {"event": "order_failed", **plan})
        else:
            plan["status"] = "planned"
            if loggers and loggers.get("analysis"):
                log_json(loggers["analysis"], {"event": "order_planned", **plan})
        actions.append(plan)

    return actions

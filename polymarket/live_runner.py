from __future__ import annotations

import time
from dataclasses import dataclass

from polymarket.clob_public import ClobPublicClient
from polymarket.execution import OrderIntent, send_heartbeat, submit_limit
from polymarket.risk import RiskLimits, RiskState, check_order
from polymarket.strategy import MeanReversionConfig, mean_reversion_signal


@dataclass(frozen=True)
class LiveLoopConfig:
    order_size: float = 5.0
    loop_seconds: int = 15
    max_loops: int = 60
    heartbeat_every_loops: int = 4


def run_live_loop(
    *,
    clob_public: ClobPublicClient,
    trading_client,
    token_id: str,
    strategy_cfg: MeanReversionConfig,
    risk_limits: RiskLimits,
    live_cfg: LiveLoopConfig,
) -> None:
    state = RiskState(position_size=0.0, realized_pnl=0.0)
    prices: list[float] = []

    for i in range(live_cfg.max_loops):
        tob = clob_public.top_of_book(token_id)
        if tob.best_bid is None or tob.best_ask is None:
            print("skip: no bid/ask")
            time.sleep(live_cfg.loop_seconds)
            continue
        fair = float((tob.best_bid + tob.best_ask) / 2)
        prices.append(fair)
        sig = mean_reversion_signal(prices, strategy_cfg)
        side = "BUY" if sig.should_buy else "SELL" if sig.should_sell else None
        print(f"loop={i} fair={fair:.4f} z={sig.zscore:.3f} side={side}")

        if side:
            px = float(tob.best_ask) if side == "BUY" else float(tob.best_bid)
            ok, reason = check_order(
                side=side,
                price=px,
                size=live_cfg.order_size,
                fair=fair,
                state=state,
                limits=risk_limits,
            )
            if ok:
                intent = OrderIntent(
                    token_id=token_id,
                    side=side,
                    price=px,
                    size=live_cfg.order_size,
                    tick_size=tob.tick_size,
                    neg_risk=tob.neg_risk,
                )
                resp = submit_limit(trading_client, intent)
                print("order_submitted", resp)
                state.position_size += live_cfg.order_size if side == "BUY" else -live_cfg.order_size
            else:
                print("blocked_by_risk", reason)

        if i % max(1, live_cfg.heartbeat_every_loops) == 0:
            hb = send_heartbeat(trading_client)
            print("heartbeat", hb)

        time.sleep(live_cfg.loop_seconds)

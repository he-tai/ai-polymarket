from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from polymarket.execution import OrderIntent, send_heartbeat, submit_limit
from polymarket.market_stream import market_bidask_stream
from polymarket.risk import RiskLimits, RiskState, check_order
from polymarket.storage import TradingStorage
from polymarket.strategy import MeanReversionConfig, mean_reversion_signal


@dataclass(frozen=True)
class MarketSelection:
    slug: str
    question: str
    outcome_label: str
    token_id: str
    tick_size: float
    neg_risk: bool


@dataclass(frozen=True)
class KillSwitchConfig:
    max_consecutive_losses: int = 4
    max_api_errors: int = 5
    max_abnormal_spread: float = 0.15
    max_stale_seconds: int = 30
    max_portfolio_loss: float = 150.0
    max_portfolio_notional: float = 500.0


@dataclass(frozen=True)
class MultiLiveConfig:
    order_size: float = 5.0
    max_events: int = 1000
    heartbeat_seconds: int = 20
    fee_bps: float = 7.0
    reconcile_every_events: int = 20


@dataclass
class _PendingOrder:
    market_token_id: str
    side: str
    intended_price: float
    intended_size: float
    accounted_size: float = 0.0


@dataclass
class _MarketState:
    selection: MarketSelection
    prices: deque[float] = field(default_factory=lambda: deque(maxlen=500))
    risk: RiskState = field(default_factory=RiskState)
    position: float = 0.0
    avg_entry: float = 0.0
    consecutive_losses: int = 0
    api_errors: int = 0
    abnormal_spread_hits: int = 0
    last_update_monotonic: float = field(default_factory=time.monotonic)

    def apply_fill(self, *, side: str, price: float, size: float) -> float:
        realized_delta = 0.0
        if side == "BUY":
            if self.position >= 0:
                total = self.avg_entry * self.position + price * size
                self.position += size
                self.avg_entry = total / max(self.position, 1e-9)
            else:
                closing = min(size, abs(self.position))
                realized_delta = (self.avg_entry - price) * closing
                self.position += size
                if self.position > 0:
                    self.avg_entry = price
        else:
            if self.position <= 0:
                total = self.avg_entry * abs(self.position) + price * size
                self.position -= size
                self.avg_entry = total / max(abs(self.position), 1e-9)
            else:
                closing = min(size, self.position)
                realized_delta = (price - self.avg_entry) * closing
                self.position -= size
                if self.position < 0:
                    self.avg_entry = price
        self.risk.position_size = self.position
        self.risk.realized_pnl += realized_delta
        if realized_delta < 0:
            self.consecutive_losses += 1
        elif realized_delta > 0:
            self.consecutive_losses = 0
        return realized_delta

    def unrealized(self, mid: float) -> float:
        if self.position > 0:
            return (mid - self.avg_entry) * self.position
        if self.position < 0:
            return (self.avg_entry - mid) * abs(self.position)
        return 0.0


def _extract_order_id(resp: Any) -> str:
    if not isinstance(resp, dict):
        return ""
    for k in ("orderID", "order_id", "id"):
        v = resp.get(k)
        if isinstance(v, str) and v:
            return v
    return ""


def _extract_filled_size(order_payload: dict[str, Any]) -> float:
    candidates = [
        order_payload.get("filled_size"),
        order_payload.get("size_matched"),
        order_payload.get("matched_size"),
        order_payload.get("filledSize"),
    ]
    for c in candidates:
        if c is None:
            continue
        try:
            return max(0.0, float(c))
        except (TypeError, ValueError):
            continue
    return 0.0


def _extract_status(order_payload: dict[str, Any]) -> str:
    for k in ("status", "state"):
        v = order_payload.get(k)
        if isinstance(v, str) and v:
            return v.lower()
    return "unknown"


def _is_terminal(status: str) -> bool:
    return status in {"filled", "cancelled", "canceled", "expired", "rejected", "matched"}


async def _reconcile_orders(
    *,
    trading_client,
    pending_orders: dict[str, _PendingOrder],
    states: dict[str, _MarketState],
    storage: TradingStorage,
    live_cfg: MultiLiveConfig,
) -> None:
    for order_id, pend in list(pending_orders.items()):
        try:
            payload = await asyncio.to_thread(trading_client.get_order, order_id)
        except Exception as exc:  # noqa: BLE001
            storage.log_event(
                {
                    "level": "ERROR",
                    "event_type": "reconcile_error",
                    "token_id": pend.market_token_id,
                    "message": str(exc),
                    "metadata": {"order_id": order_id},
                }
            )
            continue

        if not isinstance(payload, dict):
            payload = {"raw": payload}
        status = _extract_status(payload)
        filled = _extract_filled_size(payload)
        delta = max(0.0, filled - pend.accounted_size)
        st = states.get(pend.market_token_id)
        if st and delta > 0:
            fill_price = pend.intended_price
            try:
                fill_price = float(payload.get("price", fill_price))
            except (TypeError, ValueError):
                pass
            st.apply_fill(side=pend.side, price=fill_price, size=delta)
            fees = (fill_price * delta) * (live_cfg.fee_bps / 10000.0)
            st.risk.realized_pnl -= fees
            pend.accounted_size += delta

        storage.update_trade_status(order_id, status, metadata=payload)

        if _is_terminal(status):
            pending_orders.pop(order_id, None)


def _portfolio_snapshot(states: dict[str, _MarketState], mids: dict[str, float]) -> tuple[float, float, float]:
    total_realized = sum(s.risk.realized_pnl for s in states.values())
    total_unrealized = 0.0
    total_abs_notional = 0.0
    for token_id, st in states.items():
        mid = mids.get(token_id)
        if mid is None:
            continue
        total_unrealized += st.unrealized(mid)
        total_abs_notional += abs(st.position * mid)
    return total_realized, total_unrealized, total_abs_notional


async def run_multi_market_live(
    *,
    markets: list[MarketSelection],
    trading_client,
    strategy_cfg: MeanReversionConfig,
    risk_limits: RiskLimits,
    kill_cfg: KillSwitchConfig,
    live_cfg: MultiLiveConfig,
    storage: TradingStorage,
) -> None:
    states = {m.token_id: _MarketState(selection=m) for m in markets}
    token_ids = [m.token_id for m in markets]
    mids: dict[str, float] = {}
    pending_orders: dict[str, _PendingOrder] = {}
    last_hb = 0.0
    seen_events = 0

    storage.log_event(
        {
            "level": "INFO",
            "event_type": "multi_live_start",
            "message": f"starting multi-market live run for {len(markets)} markets",
            "metadata": {"token_ids": token_ids},
        }
    )

    async for upd in market_bidask_stream(token_ids):
        seen_events += 1
        st = states.get(upd.token_id)
        if st is None:
            continue
        st.last_update_monotonic = time.monotonic()
        mid = (upd.best_bid + upd.best_ask) / 2.0
        mids[upd.token_id] = mid
        st.prices.append(mid)

        if upd.spread > kill_cfg.max_abnormal_spread:
            st.abnormal_spread_hits += 1
            storage.log_event(
                {
                    "level": "WARN",
                    "event_type": "abnormal_spread",
                    "market_slug": st.selection.slug,
                    "token_id": st.selection.token_id,
                    "message": f"spread {upd.spread:.4f} > max {kill_cfg.max_abnormal_spread:.4f}",
                    "metadata": {"spread": upd.spread},
                }
            )
            if st.abnormal_spread_hits >= 3:
                raise RuntimeError(f"Kill switch: abnormal spread repeated on {st.selection.slug}")
            continue
        st.abnormal_spread_hits = 0

        now = time.monotonic()
        stale = [
            s.selection.slug for s in states.values() if now - s.last_update_monotonic > kill_cfg.max_stale_seconds
        ]
        if stale:
            raise RuntimeError(f"Kill switch: stale websocket data for markets={stale}")

        sig = mean_reversion_signal(list(st.prices), strategy_cfg)
        side = "BUY" if sig.should_buy else "SELL" if sig.should_sell else None
        if side:
            px = upd.best_ask if side == "BUY" else upd.best_bid
            ok, reason = check_order(
                side=side,
                price=px,
                size=live_cfg.order_size,
                fair=mid,
                state=st.risk,
                limits=risk_limits,
            )
            if not ok:
                storage.log_event(
                    {
                        "level": "INFO",
                        "event_type": "risk_block",
                        "market_slug": st.selection.slug,
                        "token_id": st.selection.token_id,
                        "message": reason,
                    }
                )
            else:
                intent = OrderIntent(
                    token_id=st.selection.token_id,
                    side=side,
                    price=px,
                    size=live_cfg.order_size,
                    tick_size=st.selection.tick_size,
                    neg_risk=st.selection.neg_risk,
                )
                try:
                    resp = await asyncio.to_thread(submit_limit, trading_client, intent)
                except Exception as exc:  # noqa: BLE001
                    st.api_errors += 1
                    storage.log_event(
                        {
                            "level": "ERROR",
                            "event_type": "order_error",
                            "market_slug": st.selection.slug,
                            "token_id": st.selection.token_id,
                            "message": str(exc),
                        }
                    )
                    if st.api_errors >= kill_cfg.max_api_errors:
                        raise RuntimeError(f"Kill switch: API errors on {st.selection.slug}") from exc
                else:
                    st.api_errors = 0
                    oid = _extract_order_id(resp)
                    if oid:
                        pending_orders[oid] = _PendingOrder(
                            market_token_id=st.selection.token_id,
                            side=side,
                            intended_price=px,
                            intended_size=live_cfg.order_size,
                        )
                    storage.log_trade(
                        {
                            "market_slug": st.selection.slug,
                            "token_id": st.selection.token_id,
                            "side": side,
                            "price": px,
                            "size": live_cfg.order_size,
                            "notional": px * live_cfg.order_size,
                            "fees": 0.0,
                            "impact_cost": 0.0,
                            "status": "submitted",
                            "order_id": oid,
                            "metadata": {"response": resp, "zscore": sig.zscore},
                        }
                    )

        if seen_events % max(1, live_cfg.reconcile_every_events) == 0 and pending_orders:
            await _reconcile_orders(
                trading_client=trading_client,
                pending_orders=pending_orders,
                states=states,
                storage=storage,
                live_cfg=live_cfg,
            )

        total_realized, total_unrealized, total_abs_notional = _portfolio_snapshot(states, mids)
        total_pnl = total_realized + total_unrealized
        if total_pnl <= -abs(kill_cfg.max_portfolio_loss):
            raise RuntimeError(f"Kill switch: portfolio loss breached: {total_pnl:.4f}")
        if total_abs_notional >= kill_cfg.max_portfolio_notional:
            raise RuntimeError(f"Kill switch: portfolio notional breached: {total_abs_notional:.4f}")

        for token_id, ms in states.items():
            token_mid = mids.get(token_id)
            if token_mid is None:
                continue
            st_unr = ms.unrealized(token_mid)
            storage.log_pnl(
                {
                    "market_slug": ms.selection.slug,
                    "token_id": ms.selection.token_id,
                    "position": ms.position,
                    "avg_entry": ms.avg_entry,
                    "realized_pnl": ms.risk.realized_pnl,
                    "unrealized_pnl": st_unr,
                    "total_pnl": ms.risk.realized_pnl + st_unr,
                }
            )

        if now - last_hb >= live_cfg.heartbeat_seconds:
            await asyncio.to_thread(send_heartbeat, trading_client)
            last_hb = now

        if seen_events >= live_cfg.max_events:
            storage.log_event(
                {
                    "level": "INFO",
                    "event_type": "multi_live_stop",
                    "message": f"max_events reached: {seen_events}",
                    "metadata": {
                        "pending_orders": len(pending_orders),
                        "portfolio_notional": total_abs_notional,
                        "portfolio_total_pnl": total_pnl,
                    },
                }
            )
            return

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskLimits:
    max_notional_per_trade: float = 25.0
    max_position_size: float = 200.0
    max_daily_loss: float = 50.0
    min_edge_bps: float = 10.0


@dataclass
class RiskState:
    position_size: float = 0.0
    realized_pnl: float = 0.0


def edge_bps(fair: float, execution_price: float, side: str) -> float:
    if side == "BUY":
        edge = fair - execution_price
    else:
        edge = execution_price - fair
    return edge * 10000.0


def check_order(
    *,
    side: str,
    price: float,
    size: float,
    fair: float,
    state: RiskState,
    limits: RiskLimits,
) -> tuple[bool, str]:
    notional = price * size
    if notional > limits.max_notional_per_trade:
        return False, f"notional {notional:.4f} > max_notional_per_trade {limits.max_notional_per_trade:.4f}"
    projected = state.position_size + (size if side == "BUY" else -size)
    if abs(projected) > limits.max_position_size:
        return False, f"projected position {projected:.2f} > max_position_size {limits.max_position_size:.2f}"
    if state.realized_pnl <= -abs(limits.max_daily_loss):
        return False, f"daily loss limit breached: {state.realized_pnl:.2f}"
    ebps = edge_bps(fair=fair, execution_price=price, side=side)
    if ebps < limits.min_edge_bps:
        return False, f"edge {ebps:.2f} bps < min_edge_bps {limits.min_edge_bps:.2f}"
    return True, "ok"

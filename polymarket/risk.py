from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date


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
    trade_date: str = field(default_factory=lambda: str(date.today()))

    # ------------------------------------------------------------------ #
    # 持久化：将 RiskState 写入 / 从 JSON 文件读取，按交易日自动重置        #
    # ------------------------------------------------------------------ #
    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(
                {
                    "position_size": self.position_size,
                    "realized_pnl": self.realized_pnl,
                    "trade_date": self.trade_date,
                },
                f,
                indent=2,
            )

    @classmethod
    def load(cls, path: str) -> "RiskState":
        """
        从文件加载状态。若文件不存在或日期已过，返回新的当日状态（自动重置）。
        """
        today = str(date.today())
        if not os.path.exists(path):
            return cls(trade_date=today)
        try:
            with open(path) as f:
                data = json.load(f)
            if data.get("trade_date") != today:
                # 新的交易日 → 重置 PnL，但保留持仓（持仓跨日不重置）
                return cls(
                    position_size=data.get("position_size", 0.0),
                    realized_pnl=0.0,
                    trade_date=today,
                )
            return cls(
                position_size=data.get("position_size", 0.0),
                realized_pnl=data.get("realized_pnl", 0.0),
                trade_date=today,
            )
        except Exception:
            return cls(trade_date=today)


def mid_price(best_bid: float | None, best_ask: float | None) -> float | None:
    """
    用盘口 mid-price 作为公允价格的近似。
    若盘口缺失则返回 None。
    """
    if best_bid is None or best_ask is None:
        return None
    return (best_bid + best_ask) / 2.0


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
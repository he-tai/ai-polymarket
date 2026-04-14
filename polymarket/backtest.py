from __future__ import annotations

from dataclasses import dataclass

from polymarket.risk import RiskLimits, RiskState, check_order
from polymarket.strategy import MeanReversionConfig, mean_reversion_signal


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    wins: int
    losses: int
    gross_pnl: float
    final_position: float


@dataclass(frozen=True)
class BacktestCostModel:
    slippage_bps: float = 5.0
    taker_fee_bps: float = 7.0
    maker_fee_bps: float = 0.0
    impact_bps_per_unit: float = 0.2
    use_taker: bool = True


def run_mean_reversion_backtest(
    prices: list[float],
    *,
    cfg: MeanReversionConfig,
    limits: RiskLimits,
    order_size: float,
    cost: BacktestCostModel | None = None,
) -> BacktestResult:
    cost = cost or BacktestCostModel()
    if len(prices) < cfg.window + 2:
        return BacktestResult(0, 0, 0, 0.0, 0.0)

    state = RiskState(position_size=0.0, realized_pnl=0.0)
    trades = 0
    wins = 0
    losses = 0

    # 使用中间价做简化库存记账，仅用于基础健壮性校验。
    avg_entry = 0.0
    position = 0.0

    for i in range(cfg.window, len(prices) - 1):
        hist = prices[: i + 1]
        fair = prices[i]
        nxt = prices[i + 1]
        sig = mean_reversion_signal(hist, cfg)
        side = "BUY" if sig.should_buy else "SELL" if sig.should_sell else None
        if side is None:
            continue

        slip = cost.slippage_bps / 10000.0
        impact = (cost.impact_bps_per_unit * order_size) / 10000.0
        px = fair + (slip + impact) if side == "BUY" else fair - (slip + impact)
        ok, _reason = check_order(side=side, price=px, size=order_size, fair=fair, state=state, limits=limits)
        if not ok:
            continue

        trades += 1
        fee_bps = cost.taker_fee_bps if cost.use_taker else cost.maker_fee_bps
        fees = (px * order_size) * (fee_bps / 10000.0)
        state.realized_pnl -= fees
        if side == "BUY":
            # 更新均价：多头加仓或空头回补时分别处理
            if position >= 0:
                total_cost = avg_entry * position + px * order_size
                position += order_size
                avg_entry = total_cost / max(position, 1e-9)
            else:
                closing = min(order_size, abs(position))
                pnl = (avg_entry - px) * closing
                state.realized_pnl += pnl
                wins += int(pnl > 0)
                losses += int(pnl < 0)
                position += order_size
                if position > 0:
                    avg_entry = px
        else:
            if position <= 0:
                total_cost = avg_entry * abs(position) + px * order_size
                position -= order_size
                avg_entry = total_cost / max(abs(position), 1e-9)
            else:
                closing = min(order_size, position)
                pnl = (px - avg_entry) * closing
                state.realized_pnl += pnl
                wins += int(pnl > 0)
                losses += int(pnl < 0)
                position -= order_size
                if position < 0:
                    avg_entry = px

        state.position_size = position
        # 用下一根 bar 做简化盯市，让 PnL 方向对行情变化更敏感
        state.realized_pnl += (nxt - fair) * position * 0.05

    return BacktestResult(
        trades=trades,
        wins=wins,
        losses=losses,
        gross_pnl=state.realized_pnl,
        final_position=position,
    )

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean, pstdev
from typing import Optional


@dataclass(frozen=True)
class MeanReversionConfig:
    window: int = 24
    z_entry: float = 1.2
    # TTR 过滤：距结算不足 min_hours_to_resolution 小时的市场跳过
    min_hours_to_resolution: float = 6.0
    # 趋势过滤：若价格整体变化幅度超过此阈值，视为趋势市场，不做均值回归
    trend_threshold: float = 0.15


@dataclass(frozen=True)
class Signal:
    zscore: float
    should_buy: bool
    should_sell: bool
    skipped: bool = False
    skip_reason: str = ""
    # 额外信息，供 AI 决策层参考
    mu: float = 0.0
    sigma: float = 0.0
    price_range: float = 0.0
    in_trend: bool = False


def _hours_to_resolution(end_date_iso: Optional[str]) -> Optional[float]:
    """
    将市场结算时间（ISO 格式字符串）转换为距现在的剩余小时数。
    若无法解析则返回 None（不做 TTR 过滤）。
    """
    if not end_date_iso:
        return None
    try:
        end_dt = datetime.fromisoformat(end_date_iso.replace("Z", "+00:00"))
        now = datetime.now(tz=timezone.utc)
        delta = (end_dt - now).total_seconds() / 3600.0
        return delta
    except Exception:
        return None


def mean_reversion_signal(
    prices: list[float],
    cfg: MeanReversionConfig,
    end_date_iso: Optional[str] = None,
) -> Signal:
    """
    计算均值回归信号。

    改进点：
    1. TTR 过滤：临近结算的市场跳过，避免收敛阶段产生错误信号。
    2. 趋势过滤：价格整体漂移过大时标记为趋势市场，不做均值回归。
    3. 将 mu / sigma / price_range / in_trend 暴露给 AI 决策层。
    """
    # ── TTR 过滤 ──────────────────────────────────────────────────────────
    ttr = _hours_to_resolution(end_date_iso)
    if ttr is not None and ttr < cfg.min_hours_to_resolution:
        return Signal(
            zscore=0.0,
            should_buy=False,
            should_sell=False,
            skipped=True,
            skip_reason=f"TTR {ttr:.1f}h < {cfg.min_hours_to_resolution}h，临近结算跳过",
        )

    # ── 数据量检查 ────────────────────────────────────────────────────────
    if len(prices) < max(3, cfg.window):
        return Signal(zscore=0.0, should_buy=False, should_sell=False, skipped=True, skip_reason="历史数据不足")

    lookback = prices[-cfg.window :]
    mu = fmean(lookback)
    sigma = pstdev(lookback)

    if sigma <= 1e-12:
        return Signal(zscore=0.0, should_buy=False, should_sell=False, mu=mu, sigma=0.0, skipped=True, skip_reason="价格无波动")

    # ── 趋势过滤 ──────────────────────────────────────────────────────────
    price_range = max(lookback) - min(lookback)
    in_trend = price_range > cfg.trend_threshold
    z = (prices[-1] - mu) / sigma

    # 若处于趋势行情，信号置为观望（仍返回 z-score 供 AI 参考）
    if in_trend:
        return Signal(
            zscore=z,
            should_buy=False,
            should_sell=False,
            mu=mu,
            sigma=sigma,
            price_range=price_range,
            in_trend=True,
        )

    return Signal(
        zscore=z,
        should_buy=(z <= -cfg.z_entry),
        should_sell=(z >= cfg.z_entry),
        mu=mu,
        sigma=sigma,
        price_range=price_range,
        in_trend=False,
    )


def signal_summary(signal: Signal) -> str:
    """
    将 Signal 转换为结构化文字摘要，注入到 AI 决策 prompt 中。
    """
    if signal.skipped:
        return f"[量化信号] 跳过：{signal.skip_reason}"
    trend_note = "（趋势市场，均值回归不适用）" if signal.in_trend else ""
    action = "买入" if signal.should_buy else ("卖出" if signal.should_sell else "观望")
    return (
        f"[量化信号] Z-score={signal.zscore:.3f}，均值={signal.mu:.4f}，"
        f"标准差={signal.sigma:.4f}，价格区间={signal.price_range:.4f}，"
        f"信号={action}{trend_note}"
    )
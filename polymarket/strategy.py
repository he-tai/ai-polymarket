from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(frozen=True)
class MeanReversionConfig:
    window: int = 24
    z_entry: float = 1.2


@dataclass(frozen=True)
class Signal:
    zscore: float
    should_buy: bool
    should_sell: bool


def mean_reversion_signal(prices: list[float], cfg: MeanReversionConfig) -> Signal:
    if len(prices) < max(3, cfg.window):
        return Signal(zscore=0.0, should_buy=False, should_sell=False)
    lookback = prices[-cfg.window :]
    mu = fmean(lookback)
    sigma = pstdev(lookback)
    if sigma <= 1e-12:
        return Signal(zscore=0.0, should_buy=False, should_sell=False)
    z = (prices[-1] - mu) / sigma
    # 结果代币价格位于 [0, 1]，这里采用简单均值回归：z 低买入、z 高卖出。
    return Signal(zscore=z, should_buy=(z <= -cfg.z_entry), should_sell=(z >= cfg.z_entry))

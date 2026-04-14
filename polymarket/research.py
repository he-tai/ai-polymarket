from __future__ import annotations

from dataclasses import dataclass

from polymarket.clob_public import ClobPublicClient


@dataclass(frozen=True)
class PricePoint:
    ts: int
    price: float


def load_price_series(
    clob: ClobPublicClient,
    token_id: str,
    *,
    interval: str = "1h",
    fidelity: int = 5,
) -> list[PricePoint]:
    raw = clob.get_prices_history(token_id, interval=interval, fidelity=fidelity)
    points: list[PricePoint] = []
    for row in raw:
        ts = int(row.get("t", 0))
        p = float(row.get("p", 0.0))
        if ts <= 0:
            continue
        if p < 0.0 or p > 1.0:
            continue
        points.append(PricePoint(ts=ts, price=p))
    points.sort(key=lambda x: x.ts)
    return points

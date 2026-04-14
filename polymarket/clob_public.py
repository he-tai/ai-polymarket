from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

CLOB_BASE = "https://clob.polymarket.com"


@dataclass(frozen=True)
class TopOfBook:
    best_bid: Decimal | None
    best_ask: Decimal | None
    tick_size: Decimal
    neg_risk: bool
    min_order_size: Decimal


class ClobPublicClient:
    def __init__(self, base_url: str = CLOB_BASE, timeout_s: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def get_book(self, token_id: str) -> dict[str, Any]:
        r = self._client.get("/book", params={"token_id": token_id})
        r.raise_for_status()
        return r.json()

    def get_prices_history(
        self,
        token_id: str,
        *,
        interval: str = "1h",
        fidelity: int = 5,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"market": token_id, "interval": interval, "fidelity": fidelity}
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        r = self._client.get("/prices-history", params=params)
        r.raise_for_status()
        payload = r.json()
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise TypeError("Unexpected /prices-history response shape")
        return history

    def top_of_book(self, token_id: str) -> TopOfBook:
        book = self.get_book(token_id)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = Decimal(bids[0]["price"]) if bids else None
        best_ask = Decimal(asks[0]["price"]) if asks else None
        return TopOfBook(
            best_bid=best_bid,
            best_ask=best_ask,
            tick_size=Decimal(str(book.get("tick_size", "0.01"))),
            neg_risk=bool(book.get("neg_risk", False)),
            min_order_size=Decimal(str(book.get("min_order_size", "1"))),
        )

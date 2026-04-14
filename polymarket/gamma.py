from __future__ import annotations

import json
from typing import Any

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"


class GammaClient:
    def __init__(self, base_url: str = GAMMA_BASE, timeout_s: float = 30.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def close(self) -> None:
        self._client.close()

    def get_market_by_slug(self, slug: str) -> dict[str, Any]:
        r = self._client.get("/markets/slug/" + slug)
        r.raise_for_status()
        return r.json()

    def list_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        limit: int = 50,
        offset: int = 0,
        order: str | None = "volume24hr",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "limit": limit,
            "offset": offset,
            "ascending": str(ascending).lower(),
        }
        if order:
            params["order"] = order
        r = self._client.get("/markets", params=params)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, list):
            raise TypeError("Unexpected /markets response shape")
        return data


def parse_json_list_field(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        return json.loads(value)
    return []

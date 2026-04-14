from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator

import websockets

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass(frozen=True)
class BidAskUpdate:
    token_id: str
    best_bid: float
    best_ask: float
    spread: float
    timestamp_ms: int


def _as_bid_ask(obj: dict) -> BidAskUpdate | None:
    if obj.get("event_type") != "best_bid_ask":
        return None
    token_id = str(obj.get("asset_id", ""))
    if not token_id:
        return None
    bid = float(obj.get("best_bid", 0.0))
    ask = float(obj.get("best_ask", 0.0))
    spr = float(obj.get("spread", ask - bid))
    ts = int(obj.get("timestamp", 0))
    if bid <= 0.0 or ask <= 0.0:
        return None
    return BidAskUpdate(token_id=token_id, best_bid=bid, best_ask=ask, spread=spr, timestamp_ms=ts)


async def market_bidask_stream(token_ids: list[str]) -> AsyncIterator[BidAskUpdate]:
    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        await ws.send(
            json.dumps(
                {
                    "assets_ids": token_ids,
                    "type": "market",
                    "initial_dump": True,
                    "level": 2,
                    "custom_feature_enabled": True,
                }
            )
        )

        async def _send_ping() -> None:
            while True:
                await asyncio.sleep(10)
                await ws.send("PING")

        ping_task = asyncio.create_task(_send_ping())
        try:
            async for raw in ws:
                if raw == "PONG":
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                # 服务端消息可能是单个对象，也可能是对象数组
                objs = payload if isinstance(payload, list) else [payload]
                for obj in objs:
                    if not isinstance(obj, dict):
                        continue
                    upd = _as_bid_ask(obj)
                    if upd:
                        yield upd
        finally:
            ping_task.cancel()

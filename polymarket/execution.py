from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

from polymarket.config import LiveTradingConfig

Side = Literal["BUY", "SELL"]

_TICK_LITERALS = ("0.1", "0.01", "0.001", "0.0001")


def tick_size_literal(tick: Decimal) -> str:
    for lit in _TICK_LITERALS:
        if Decimal(lit) == tick:
            return lit
    best = min(_TICK_LITERALS, key=lambda lit: abs(Decimal(lit) - tick))
    return best


@dataclass(frozen=True)
class OrderIntent:
    token_id: str
    side: Side
    price: float
    size: float
    tick_size: Decimal
    neg_risk: bool


@dataclass
class TrackedOrder:
    """
    P2 修复：追踪已提交订单的状态，支持 TTL 超时撤单。
    """
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    submitted_at: float = field(default_factory=time.time)
    status: str = "open"  # open | filled | cancelled | expired

    def is_expired(self, ttl_seconds: float) -> bool:
        return self.status == "open" and (time.time() - self.submitted_at) > ttl_seconds

    def age_seconds(self) -> float:
        return time.time() - self.submitted_at


def build_trading_client(cfg: LiveTradingConfig) -> ClobClient:
    bootstrap = ClobClient(host="https://clob.polymarket.com", chain_id=137, key=cfg.private_key)
    creds = bootstrap.create_or_derive_api_creds()
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=137,
        key=cfg.private_key,
        creds=creds,
        signature_type=cfg.signature_type,
        funder=cfg.funder_address,
    )


def submit_limit(client: ClobClient, intent: OrderIntent) -> Any:
    tick_lit = tick_size_literal(intent.tick_size)
    order = OrderArgs(
        token_id=intent.token_id,
        price=float(intent.price),
        size=float(intent.size),
        side=intent.side,
    )
    options = PartialCreateOrderOptions(tick_size=tick_lit, neg_risk=intent.neg_risk)
    return client.create_and_post_order(order, options)


def cancel_order(client: ClobClient, order_id: str) -> Any:
    """
    P2 修复：撤销指定订单。
    """
    return client.cancel(order_id)


def cancel_expired_orders(
    client: ClobClient,
    tracked_orders: list[TrackedOrder],
    ttl_seconds: float = 300.0,
) -> list[TrackedOrder]:
    """
    P2 修复：扫描已追踪的订单，撤销所有超过 TTL 的未成交订单。
    返回更新后的订单列表。
    """
    updated: list[TrackedOrder] = []
    for order in tracked_orders:
        if order.is_expired(ttl_seconds):
            try:
                cancel_order(client, order.order_id)
                order.status = "cancelled"
            except Exception:
                order.status = "expired"
        updated.append(order)
    return updated


def send_heartbeat(client: ClobClient) -> Any:
    return client.post_heartbeat()
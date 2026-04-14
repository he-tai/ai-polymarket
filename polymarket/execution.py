from __future__ import annotations

from dataclasses import dataclass
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
    # 按数值距离选择最接近的可用 tick
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


def send_heartbeat(client: ClobClient) -> Any:
    return client.post_heartbeat()

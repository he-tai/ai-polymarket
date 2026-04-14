from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from polymarket.gamma import parse_json_list_field


@dataclass(frozen=True)
class OutcomeLeg:
    label: str
    token_id: str


def outcome_legs(market: dict[str, Any]) -> list[OutcomeLeg]:
    outcomes = parse_json_list_field(market.get("outcomes"))
    token_ids = parse_json_list_field(market.get("clobTokenIds"))
    if len(outcomes) != len(token_ids):
        raise ValueError("Market outcomes and clobTokenIds length mismatch")
    legs: list[OutcomeLeg] = []
    for label, token_id in zip(outcomes, token_ids, strict=True):
        legs.append(OutcomeLeg(label=str(label), token_id=str(token_id)))
    return legs


def pretty_market_header(market: dict[str, Any]) -> str:
    q = market.get("question", "")
    slug = market.get("slug", "")
    return f"{q} ({slug})"

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class LiveTradingConfig:
    private_key: str
    funder_address: str
    signature_type: int


def live_trading_config_from_env() -> LiveTradingConfig | None:
    key = os.getenv("PRIVATE_KEY", "").strip()
    funder = os.getenv("FUNDER_ADDRESS", "").strip()
    if not key or not funder:
        return None
    sig = int(os.getenv("SIGNATURE_TYPE", "2"))
    return LiveTradingConfig(private_key=key, funder_address=funder, signature_type=sig)


@dataclass(frozen=True)
class DeepSeekConfig:
    api_key: str
    model: str
    base_url: str


def deepseek_config_from_env() -> DeepSeekConfig | None:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return None
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip() or "https://api.deepseek.com"
    return DeepSeekConfig(api_key=api_key, model=model, base_url=base_url)


def database_url_from_env() -> str:
    return os.getenv("DATABASE_URL", "sqlite:///trading.db").strip() or "sqlite:///trading.db"

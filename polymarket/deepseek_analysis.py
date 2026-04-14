from __future__ import annotations

from dataclasses import dataclass

import httpx

from polymarket.config import DeepSeekConfig


@dataclass(frozen=True)
class AnalysisInput:
    market_question: str
    market_slug: str
    outcome_label: str
    history_points: int
    price_min: float
    price_max: float
    last_price: float
    spread: float | None
    backtest_trades: int
    backtest_win_rate: float
    backtest_gross_pnl: float
    backtest_final_position: float


def build_prompt(payload: AnalysisInput) -> str:
    return (
        "You are a quantitative trading research assistant.\n"
        "Analyze the market data and backtest stats below. Provide:\n"
        "1) Market regime diagnosis\n"
        "2) Strategy weaknesses and overfitting risks\n"
        "3) Parameter improvement suggestions\n"
        "4) Risk-control recommendations\n"
        "5) Clear go/no-go conclusion for next live test (small size only)\n\n"
        f"Market: {payload.market_question}\n"
        f"Slug: {payload.market_slug}\n"
        f"Outcome: {payload.outcome_label}\n"
        f"History points: {payload.history_points}\n"
        f"Price min/max/last: {payload.price_min:.6f}/{payload.price_max:.6f}/{payload.last_price:.6f}\n"
        f"Current spread: {payload.spread if payload.spread is not None else 'N/A'}\n"
        f"Backtest trades: {payload.backtest_trades}\n"
        f"Backtest win rate: {payload.backtest_win_rate:.2f}%\n"
        f"Backtest gross pnl: {payload.backtest_gross_pnl:.6f}\n"
        f"Backtest final position: {payload.backtest_final_position:.4f}\n"
    )


def request_analysis(cfg: DeepSeekConfig, prompt: str, timeout_s: float = 60.0) -> str:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": cfg.model,
        "messages": [
            {
                "role": "system",
                "content": "You are a careful quant reviewer. Do not guarantee profits. Focus on risk-aware analysis.",
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    with httpx.Client(timeout=timeout_s) as client:
        r = client.post(url, headers=headers, json=body)
        r.raise_for_status()
        data = r.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("DeepSeek returned no choices")
    msg = choices[0].get("message", {})
    content = msg.get("content", "")
    if not content:
        raise ValueError("DeepSeek returned empty content")
    return str(content)

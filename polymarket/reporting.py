from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from polymarket.storage import TradingStorage


@dataclass(frozen=True)
class ReportPaths:
    markdown_path: str
    trades_csv_path: str
    pnl_csv_path: str


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})


def generate_attribution_report(storage: TradingStorage, *, days: int, out_dir: str) -> ReportPaths:
    trades = storage.fetch_trades_since(days)
    pnls = storage.fetch_pnl_since(days)
    events = storage.fetch_events_since(days)

    by_market: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "trades": 0.0,
            "buy_notional": 0.0,
            "sell_notional": 0.0,
            "fees": 0.0,
            "impact_cost": 0.0,
        }
    )
    status_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        m = str(t.get("market_slug", "unknown"))
        side = str(t.get("side", "")).upper()
        notional = _to_float(t.get("notional"))
        by_market[m]["trades"] += 1
        if side == "BUY":
            by_market[m]["buy_notional"] += notional
        elif side == "SELL":
            by_market[m]["sell_notional"] += notional
        by_market[m]["fees"] += _to_float(t.get("fees"))
        by_market[m]["impact_cost"] += _to_float(t.get("impact_cost"))
        status_counts[str(t.get("status", "unknown"))] += 1

    latest_pnl_by_market: dict[str, float] = {}
    for p in pnls:
        latest_pnl_by_market[str(p.get("market_slug", "unknown"))] = _to_float(p.get("total_pnl"))

    now_tag = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = Path(out_dir)
    trades_csv = out / f"trades-{now_tag}.csv"
    pnl_csv = out / f"pnl-{now_tag}.csv"
    md_path = out / f"attribution-{now_tag}.md"

    trade_fields = [
        "ts",
        "market_slug",
        "token_id",
        "side",
        "price",
        "size",
        "notional",
        "fees",
        "impact_cost",
        "status",
        "order_id",
    ]
    pnl_fields = [
        "ts",
        "market_slug",
        "token_id",
        "position",
        "avg_entry",
        "realized_pnl",
        "unrealized_pnl",
        "total_pnl",
    ]
    _write_csv(trades_csv, trades, trade_fields)
    _write_csv(pnl_csv, pnls, pnl_fields)

    total_trades = int(sum(v["trades"] for v in by_market.values()))
    total_fees = sum(v["fees"] for v in by_market.values())
    total_impact = sum(v["impact_cost"] for v in by_market.values())
    filled = status_counts.get("filled", 0) + status_counts.get("matched", 0)
    fill_rate = (filled / total_trades * 100.0) if total_trades else 0.0
    latest_portfolio_pnl = sum(latest_pnl_by_market.values())

    lines = [
        f"# Attribution Report ({days}d)",
        "",
        f"- Generated at (UTC): {datetime.now(tz=timezone.utc).isoformat()}",
        f"- Trades: {total_trades}",
        f"- Fill rate (status-based): {fill_rate:.2f}%",
        f"- Total fees: {total_fees:.4f}",
        f"- Total impact_cost (logged): {total_impact:.4f}",
        f"- Latest portfolio total_pnl snapshot sum: {latest_portfolio_pnl:.4f}",
        f"- Events logged: {len(events)}",
        "",
        "## By Market",
        "",
        "| market | trades | buy_notional | sell_notional | fees | impact_cost | latest_total_pnl |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for market, v in sorted(by_market.items(), key=lambda kv: kv[1]["trades"], reverse=True):
        lines.append(
            f"| {market} | {int(v['trades'])} | {v['buy_notional']:.4f} | {v['sell_notional']:.4f} "
            f"| {v['fees']:.4f} | {v['impact_cost']:.4f} | {latest_pnl_by_market.get(market, 0.0):.4f} |"
        )
    lines += [
        "",
        "## Trade Status Counts",
        "",
        "| status | count |",
        "|---|---:|",
    ]
    for status, cnt in sorted(status_counts.items(), key=lambda kv: kv[1], reverse=True):
        lines.append(f"| {status} | {cnt} |")
    lines += [
        "",
        "## Data Quality Checks",
        "",
        f"- Missing order_id rows: {sum(1 for t in trades if not t.get('order_id'))}",
        f"- Zero/negative notional rows: {sum(1 for t in trades if _to_float(t.get('notional')) <= 0)}",
        f"- PnL snapshots count: {len(pnls)}",
        "",
    ]

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return ReportPaths(
        markdown_path=str(md_path),
        trades_csv_path=str(trades_csv),
        pnl_csv_path=str(pnl_csv),
    )

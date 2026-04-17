from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from datetime import timedelta

from sqlalchemy import JSON, Column, DateTime, Float, Integer, MetaData, String, Table, create_engine, insert, select, update
from sqlalchemy.engine import Engine


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class TradingStorage:
    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(database_url, future=True)
        self.meta = MetaData()
        self.trades = Table(
            "trades",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), default=_utcnow, nullable=False),
            Column("market_slug", String(256), nullable=False),
            Column("token_id", String(128), nullable=False),
            Column("side", String(8), nullable=False),
            Column("price", Float, nullable=False),
            Column("size", Float, nullable=False),
            Column("notional", Float, nullable=False),
            Column("fees", Float, nullable=False, default=0.0),
            Column("impact_cost", Float, nullable=False, default=0.0),
            Column("status", String(32), nullable=False, default="submitted"),
            Column("order_id", String(256), nullable=True),
            Column("metadata", JSON, nullable=True),
        )
        self.events = Table(
            "events",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), default=_utcnow, nullable=False),
            Column("level", String(16), nullable=False),
            Column("event_type", String(64), nullable=False),
            Column("market_slug", String(256), nullable=True),
            Column("token_id", String(128), nullable=True),
            Column("message", String(2048), nullable=False),
            Column("metadata", JSON, nullable=True),
        )
        self.pnl = Table(
            "pnl_snapshots",
            self.meta,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("ts", DateTime(timezone=True), default=_utcnow, nullable=False),
            Column("market_slug", String(256), nullable=False),
            Column("token_id", String(128), nullable=False),
            Column("position", Float, nullable=False),
            Column("avg_entry", Float, nullable=False),
            Column("realized_pnl", Float, nullable=False),
            Column("unrealized_pnl", Float, nullable=False),
            Column("total_pnl", Float, nullable=False),
        )
        self.meta.create_all(self.engine)

    def _insert(self, table: Table, values: dict[str, Any]) -> None:
        with self.engine.begin() as conn:
            conn.execute(insert(table).values(**values))

    def log_trade(self, values: dict[str, Any]) -> None:
        self._insert(self.trades, values)

    def log_event(self, values: dict[str, Any]) -> None:
        self._insert(self.events, values)

    def log_pnl(self, values: dict[str, Any]) -> None:
        self._insert(self.pnl, values)

    def update_trade_status(self, order_id: str, status: str, metadata: dict[str, Any] | None = None) -> None:
        if not order_id:
            return
        with self.engine.begin() as conn:
            conn.execute(
                update(self.trades)
                .where(self.trades.c.order_id == order_id)
                .values(status=status, metadata=metadata)
            )

    def fetch_trades_since(self, days: int) -> list[dict[str, Any]]:
        since = _utcnow() - timedelta(days=max(1, days))
        stmt = select(self.trades).where(self.trades.c.ts >= since).order_by(self.trades.c.ts.asc())
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        out: list[dict[str, Any]] = []
        for r in rows:
            item = dict(r)
            item["source_type"] = self._trade_source_type(item)
            out.append(item)
        return out

    def fetch_events_since(self, days: int) -> list[dict[str, Any]]:
        since = _utcnow() - timedelta(days=max(1, days))
        stmt = select(self.events).where(self.events.c.ts >= since).order_by(self.events.c.ts.asc())
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def fetch_pnl_since(self, days: int) -> list[dict[str, Any]]:
        since = _utcnow() - timedelta(days=max(1, days))
        stmt = select(self.pnl).where(self.pnl.c.ts >= since).order_by(self.pnl.c.ts.asc())
        with self.engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    def fetch_latest_positions(self) -> list[dict[str, Any]]:
        # 返回每个 token 的最新快照，并补充手动/自动持仓拆分
        pnl_latest: dict[str, dict[str, Any]] = {}
        for row in self.fetch_pnl_since(days=30):
            token_id = str(row.get("token_id", ""))
            ts = row.get("ts")
            if not token_id:
                continue
            if token_id not in pnl_latest or (ts is not None and ts > pnl_latest[token_id].get("ts")):
                pnl_latest[token_id] = row

        trade_positions = self._positions_from_trades(days=30)
        token_ids = set(pnl_latest.keys()) | set(trade_positions.keys())
        result: list[dict[str, Any]] = []
        for token_id in token_ids:
            base = dict(pnl_latest.get(token_id, {}))
            tp = trade_positions.get(token_id, {})
            base.setdefault("market_slug", tp.get("market_slug", ""))
            base.setdefault("token_id", token_id)
            base.setdefault("position", tp.get("position", 0.0))
            base.setdefault("realized_pnl", 0.0)
            base.setdefault("unrealized_pnl", 0.0)
            base.setdefault("total_pnl", 0.0)
            base["manual_position"] = tp.get("manual_position", 0.0)
            base["auto_position"] = tp.get("auto_position", 0.0)
            # 过滤空仓
            if abs(float(base.get("position", 0.0) or 0.0)) <= 1e-9 and abs(float(base["manual_position"])) <= 1e-9 and abs(float(base["auto_position"])) <= 1e-9:
                continue
            result.append(base)

        result.sort(key=lambda x: abs(float(x.get("position", 0.0) or 0.0)), reverse=True)
        return result

    @staticmethod
    def _trade_source_type(trade: dict[str, Any]) -> str:
        meta = trade.get("metadata")
        source = ""
        if isinstance(meta, dict):
            source = str(meta.get("source", "")).lower()
        if source.startswith("dashboard_") or source.startswith("manual"):
            return "manual"
        if source.startswith("auto"):
            return "auto"
        # 兼容历史数据：带 zscore 的多为策略自动单
        if isinstance(meta, dict) and "zscore" in meta:
            return "auto"
        return "manual"

    def _positions_from_trades(self, *, days: int) -> dict[str, dict[str, Any]]:
        trades = self.fetch_trades_since(days=days)
        by_token: dict[str, dict[str, Any]] = {}
        for tr in trades:
            token_id = str(tr.get("token_id", ""))
            if not token_id:
                continue
            side = str(tr.get("side", "")).upper()
            size = float(tr.get("size", 0.0) or 0.0)
            signed = size if side == "BUY" else (-size if side == "SELL" else 0.0)
            if abs(signed) <= 1e-12:
                continue
            source_type = self._trade_source_type(tr)
            slot = by_token.setdefault(
                token_id,
                {
                    "market_slug": str(tr.get("market_slug", "")),
                    "position": 0.0,
                    "manual_position": 0.0,
                    "auto_position": 0.0,
                },
            )
            slot["position"] += signed
            if source_type == "auto":
                slot["auto_position"] += signed
            else:
                slot["manual_position"] += signed
        return by_token

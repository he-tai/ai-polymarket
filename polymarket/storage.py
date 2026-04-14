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
        return [dict(r) for r in rows]

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
        # 读取每个 market_slug 的最新持仓快照
        all_rows = self.fetch_pnl_since(days=30)
        latest: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            slug = str(row.get("market_slug", ""))
            ts = row.get("ts")
            if not slug:
                continue
            if slug not in latest or (ts is not None and ts > latest[slug].get("ts")):
                latest[slug] = row
        return list(latest.values())

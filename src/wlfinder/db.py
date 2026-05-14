"""SQLite persistence (aiosqlite): attempts, deployments, whitelist meta."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any

import aiosqlite
import structlog

from wlfinder.models import Attempt, SuccessfulDeployment

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  hoster TEXT NOT NULL,
  region TEXT,
  server_id TEXT NOT NULL,
  ipv4 TEXT NOT NULL,
  ipv6 TEXT,
  hit INTEGER NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  cost_estimate_rub REAL,
  raw_create TEXT,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_hoster_hit ON attempts(hoster, hit);

CREATE TABLE IF NOT EXISTS whitelist_cache_meta (
  source_name TEXT PRIMARY KEY,
  last_fetched TEXT NOT NULL,
  network_count INTEGER NOT NULL,
  sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS successful_deployments (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  hoster TEXT NOT NULL,
  server_id TEXT NOT NULL,
  ipv4 TEXT NOT NULL,
  proxy_type TEXT NOT NULL,
  client_config_path TEXT
);
"""


class Database:
    """Async SQLite wrapper. Use as an async context manager."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path).expanduser()
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> Database:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()
        log.debug("db.connected", path=str(self._path))
        return self

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def __aenter__(self) -> Database:
        return await self.connect()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @property
    def _db(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() was not called")
        return self._conn

    async def record_attempt(self, attempt: Attempt) -> int:
        cur = await self._db.execute(
            """INSERT INTO attempts
               (ts, hoster, region, server_id, ipv4, ipv6, hit, deleted,
                cost_estimate_rub, raw_create, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt.ts.isoformat(),
                attempt.hoster,
                attempt.region,
                attempt.server_id,
                attempt.ipv4,
                attempt.ipv6,
                int(attempt.hit),
                int(attempt.deleted),
                attempt.cost_estimate_rub,
                json.dumps(attempt.raw_create) if attempt.raw_create else None,
                attempt.notes,
            ),
        )
        await self._db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def mark_deleted(self, attempt_id: int) -> None:
        await self._db.execute("UPDATE attempts SET deleted = 1 WHERE id = ?", (attempt_id,))
        await self._db.commit()

    async def record_deployment(self, dep: SuccessfulDeployment) -> int:
        cur = await self._db.execute(
            """INSERT INTO successful_deployments
               (ts, hoster, server_id, ipv4, proxy_type, client_config_path)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                dep.ts.isoformat(),
                dep.hoster,
                dep.server_id,
                dep.ipv4,
                dep.proxy_type,
                dep.client_config_path,
            ),
        )
        await self._db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def upsert_whitelist_meta(
        self,
        source_name: str,
        last_fetched: datetime,
        network_count: int,
        sha256: str,
    ) -> None:
        await self._db.execute(
            """INSERT INTO whitelist_cache_meta
               (source_name, last_fetched, network_count, sha256)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source_name) DO UPDATE SET
                 last_fetched = excluded.last_fetched,
                 network_count = excluded.network_count,
                 sha256 = excluded.sha256""",
            (source_name, last_fetched.isoformat(), network_count, sha256),
        )
        await self._db.commit()

    async def hit_rate_by_hoster(self) -> list[dict[str, Any]]:
        cur = await self._db.execute(
            """SELECT hoster,
                      COUNT(*) AS attempts,
                      COALESCE(SUM(hit), 0) AS hits
               FROM attempts
               GROUP BY hoster
               ORDER BY hoster"""
        )
        rows = await cur.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            attempts = int(r["attempts"])
            hits = int(r["hits"])
            result.append(
                {
                    "hoster": r["hoster"],
                    "attempts": attempts,
                    "hits": hits,
                    "hit_rate": hits / attempts if attempts else 0.0,
                }
            )
        return result

    async def count_attempts(self) -> int:
        cur = await self._db.execute("SELECT COUNT(*) AS n FROM attempts")
        row = await cur.fetchone()
        return int(row["n"]) if row is not None else 0

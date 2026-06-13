"""SQLite persistence for events and monitor results (stdlib sqlite3).

One connection shared across threads; all access serialized by a lock. Rows
are pruned so the database stays small on long-lived devices.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS monitor_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    url TEXT NOT NULL,
    status_code INTEGER,
    latency_ms REAL,
    ok INTEGER NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_monitor_ts ON monitor_results(ts);
CREATE TABLE IF NOT EXISTS urc_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    raw TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_urc_ts ON urc_log(ts);
CREATE TABLE IF NOT EXISTS identity_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    iccid TEXT,
    imsi TEXT,
    imei TEXT,
    operator TEXT,
    registration TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identity_ts ON identity_history(ts);
"""

MAX_ROWS = 5000


class Database:
    def __init__(self, path: Path | str) -> None:
        if isinstance(path, str) and path != ":memory:":
            path = Path(path)
        if isinstance(path, Path):
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add_event(
        self, level: str, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (ts, level, kind, message, data) VALUES (?, ?, ?, ?, ?)",
                (time.time(), level, kind, message, json.dumps(data) if data else None),
            )
            self._prune("events")
            self._conn.commit()

    def recent_events(self, limit: int = 200, kind: str | None = None) -> list[dict]:
        query = "SELECT * FROM events"
        params: tuple = ()
        if kind:
            query += " WHERE kind = ?"
            params = (kind,)
        query += " ORDER BY id DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, params + (limit,)).fetchall()
        return [dict(r) for r in rows]

    def add_monitor_result(
        self,
        url: str,
        status_code: int | None,
        latency_ms: float | None,
        ok: bool,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO monitor_results (ts, url, status_code, latency_ms, ok, error)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), url, status_code, latency_ms, int(ok), error),
            )
            self._prune("monitor_results")
            self._conn.commit()

    def recent_monitor_results(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM monitor_results ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def add_urc(self, kind: str, raw: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO urc_log (ts, kind, raw, data) VALUES (?, ?, ?, ?)",
                (time.time(), kind, raw, json.dumps(data) if data else None),
            )
            self._prune("urc_log")
            self._conn.commit()

    def recent_urcs(self, limit: int = 300) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM urc_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def add_identity(
        self,
        iccid: str | None,
        imsi: str | None,
        imei: str | None,
        operator: str | None,
        registration: str | None,
        reason: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO identity_history"
                " (ts, iccid, imsi, imei, operator, registration, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), iccid, imsi, imei, operator, registration, reason),
            )
            self._prune("identity_history")
            self._conn.commit()

    def recent_identity(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM identity_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def _prune(self, table: str) -> None:
        # Caller holds the lock.
        self._conn.execute(
            f"DELETE FROM {table} WHERE id <= ("  # noqa: S608 — table is internal
            f"SELECT MAX(id) FROM {table}) - ?",
            (MAX_ROWS,),
        )

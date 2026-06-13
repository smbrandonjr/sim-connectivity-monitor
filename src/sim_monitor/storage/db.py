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
CREATE TABLE IF NOT EXISTS sms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,         -- 'in' | 'out'
    peer TEXT,
    body TEXT,
    encoding TEXT,
    status TEXT,                     -- 'unread' | 'read' | 'sent'
    modem_indices TEXT,              -- JSON list of modem storage indices (inbound)
    parts INTEGER DEFAULT 1,
    raw_pdu TEXT,
    dedup TEXT                       -- stable content key (inbound): peer|ts|body
);
CREATE INDEX IF NOT EXISTS idx_sms_ts ON sms(ts);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    rssi INTEGER, rsrp INTEGER, rsrq INTEGER, sinr INTEGER,
    rat TEXT, band INTEGER, cell_id TEXT, pci INTEGER,
    earfcn INTEGER, tac TEXT, operator_numeric TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE TABLE IF NOT EXISTS sim_names (
    iccid TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns introduced after a DB was first created (caller holds lock)."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sms)")}
        if "dedup" not in cols:
            self._conn.execute("ALTER TABLE sms ADD COLUMN dedup TEXT")

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

    def recent_monitor_results(self, limit: int = 200, offset: int = 0) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM monitor_results ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]

    def count_monitor_results(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) AS n FROM monitor_results"
            ).fetchone()["n"]

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

    # ── SMS ──────────────────────────────────────────────────────────────
    def upsert_inbound_sms(self, rows: list[dict[str, Any]]) -> int:
        """Sync the inbound set to the modem's current contents, preserving our
        app-managed read state across refreshes (keyed by a stable `dedup`).
        New messages are inserted as 'unread'. Returns the count of NEW messages."""
        with self._lock:
            current = [r["dedup"] for r in rows]
            if current:
                marks = ",".join("?" * len(current))
                self._conn.execute(
                    f"DELETE FROM sms WHERE direction='in' AND (dedup IS NULL OR "  # noqa: S608
                    f"dedup NOT IN ({marks}))",
                    current,
                )
            else:
                self._conn.execute("DELETE FROM sms WHERE direction='in'")
            new_count = 0
            for r in rows:
                existing = self._conn.execute(
                    "SELECT id FROM sms WHERE direction='in' AND dedup=?", (r["dedup"],)
                ).fetchone()
                if existing:
                    self._conn.execute(
                        "UPDATE sms SET modem_indices=?, parts=? WHERE id=?",
                        (json.dumps(r["modem_indices"]), r.get("parts", 1), existing["id"]),
                    )
                else:
                    new_count += 1
                    self._conn.execute(
                        "INSERT INTO sms (ts, direction, peer, body, encoding, status,"
                        " modem_indices, parts, raw_pdu, dedup)"
                        " VALUES (?, 'in', ?, ?, ?, 'unread', ?, ?, ?, ?)",
                        (
                            r["ts"], r["peer"], r["body"], r["encoding"],
                            json.dumps(r["modem_indices"]), r.get("parts", 1),
                            r.get("raw_pdu"), r["dedup"],
                        ),
                    )
            self._conn.commit()
        return new_count

    def mark_inbound_read(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sms SET status='read' WHERE direction='in' AND status='unread'"
            )
            self._conn.commit()

    def add_sent_sms(self, peer: str, body: str, parts: int = 1) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sms (ts, direction, peer, body, encoding, status, parts)"
                " VALUES (?, 'out', ?, ?, 'gsm7', 'sent', ?)",
                (time.time(), peer, body, parts),
            )
            self._prune("sms")
            self._conn.commit()

    def recent_sms(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sms ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["modem_indices"] = json.loads(d["modem_indices"]) if d["modem_indices"] else []
            result.append(d)
        return result

    def get_sms(self, sms_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM sms WHERE id = ?", (sms_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["modem_indices"] = json.loads(d["modem_indices"]) if d["modem_indices"] else []
        return d

    def delete_sms_row(self, sms_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM sms WHERE id = ?", (sms_id,))
            self._conn.commit()

    # ── telemetry ────────────────────────────────────────────────────────
    _TELEMETRY_COLS = (
        "rssi", "rsrp", "rsrq", "sinr", "rat", "band",
        "cell_id", "pci", "earfcn", "tac", "operator_numeric",
    )

    def add_telemetry(self, sample: dict[str, Any]) -> None:
        values = [sample.get(c) for c in self._TELEMETRY_COLS]
        with self._lock:
            self._conn.execute(
                f"INSERT INTO telemetry (ts, {', '.join(self._TELEMETRY_COLS)})"
                f" VALUES (?, {', '.join('?' * len(self._TELEMETRY_COLS))})",
                (time.time(), *values),
            )
            self._prune("telemetry")
            self._conn.commit()

    def recent_telemetry(self, limit: int = 500) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM telemetry ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── per-SIM names (keyed by ICCID) ───────────────────────────────────
    def get_sim_name(self, iccid: str | None) -> str | None:
        if not iccid:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM sim_names WHERE iccid = ?", (iccid,)
            ).fetchone()
        return row["name"] if row else None

    def set_sim_name(self, iccid: str, name: str) -> None:
        with self._lock:
            if name:
                self._conn.execute(
                    "INSERT INTO sim_names (iccid, name) VALUES (?, ?)"
                    " ON CONFLICT(iccid) DO UPDATE SET name = excluded.name",
                    (iccid, name),
                )
            else:  # empty name clears it
                self._conn.execute("DELETE FROM sim_names WHERE iccid = ?", (iccid,))
            self._conn.commit()

    # ── key/value settings (JSON values) ─────────────────────────────────
    def get_setting(self, key: str) -> Any | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else None

    def set_setting(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )
            self._conn.commit()

    def count_unread_sms(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sms WHERE direction='in' AND status='unread'"
            ).fetchone()
        return row["n"]

    def _prune(self, table: str) -> None:
        # Caller holds the lock.
        self._conn.execute(
            f"DELETE FROM {table} WHERE id <= ("  # noqa: S608 — table is internal
            f"SELECT MAX(id) FROM {table}) - ?",
            (MAX_ROWS,),
        )

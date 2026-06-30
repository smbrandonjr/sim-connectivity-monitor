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
    error TEXT,
    interface TEXT
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
CREATE TABLE IF NOT EXISTS udp_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    direction TEXT NOT NULL,         -- 'in' | 'out'
    port INTEGER NOT NULL,           -- local port the datagram hit / replied from
    peer TEXT NOT NULL,              -- 'ip:port' of the remote
    body TEXT,                       -- UTF-8 decode (NULL if not decodable)
    body_hex TEXT,                   -- hex of raw bytes
    length INTEGER NOT NULL,
    matched_rule TEXT                -- rule that fired (inbound), reply label (outbound)
);
CREATE INDEX IF NOT EXISTS idx_udp_ts ON udp_messages(ts);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    rssi INTEGER, rsrp INTEGER, rsrq INTEGER, sinr INTEGER,
    rat TEXT, band INTEGER, cell_id TEXT, pci INTEGER,
    earfcn INTEGER, tac TEXT, operator_numeric TEXT
);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE TABLE IF NOT EXISTS connectivity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    up INTEGER NOT NULL,         -- 1 = cellular CONNECTED, 0 = down
    state TEXT,                  -- daemon state at the edge
    detail TEXT                  -- reason (e.g. last_error) when going down
);
CREATE INDEX IF NOT EXISTS idx_connectivity_ts ON connectivity(ts);
CREATE TABLE IF NOT EXISTS icmp_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    interface TEXT NOT NULL,
    target TEXT NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    loss_pct REAL NOT NULL,
    rtt_avg_ms REAL,
    rtt_min_ms REAL,
    rtt_max_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_icmp_samples_ts ON icmp_samples(ts);
CREATE TABLE IF NOT EXISTS icmp_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_start REAL NOT NULL,
    period TEXT NOT NULL,            -- 'hour' | 'day'
    interface TEXT NOT NULL,
    target TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    loss_pct REAL NOT NULL,
    rtt_avg_ms REAL,
    rtt_min_ms REAL,
    rtt_max_ms REAL,
    UNIQUE(period, bucket_start, interface, target)
);
CREATE INDEX IF NOT EXISTS idx_icmp_rollups_bucket ON icmp_rollups(period, bucket_start);
CREATE TABLE IF NOT EXISTS http_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    interface TEXT NOT NULL,
    target TEXT NOT NULL,            -- the http(s):// URL
    ok INTEGER NOT NULL,             -- 1 = success (status < 400), 0 = failure
    status_code INTEGER,             -- HTTP status; NULL on timeout/conn error
    latency_ms REAL                  -- request time; NULL on failure
);
CREATE INDEX IF NOT EXISTS idx_http_samples_ts ON http_samples(ts);
CREATE TABLE IF NOT EXISTS http_rollups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bucket_start REAL NOT NULL,
    period TEXT NOT NULL,            -- 'hour' | 'day'
    interface TEXT NOT NULL,
    target TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    sent INTEGER NOT NULL,
    received INTEGER NOT NULL,
    loss_pct REAL NOT NULL,
    rtt_avg_ms REAL,
    rtt_min_ms REAL,
    rtt_max_ms REAL,
    status_code INTEGER,             -- representative (last) status in the bucket
    UNIQUE(period, bucket_start, interface, target)
);
CREATE INDEX IF NOT EXISTS idx_http_rollups_bucket ON http_rollups(period, bucket_start);
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
        mcols = {r["name"] for r in self._conn.execute("PRAGMA table_info(monitor_results)")}
        if "interface" not in mcols:
            self._conn.execute("ALTER TABLE monitor_results ADD COLUMN interface TEXT")

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
        interface: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO monitor_results"
                " (ts, url, status_code, latency_ms, ok, error, interface)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (time.time(), url, status_code, latency_ms, int(ok), error, interface),
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
    def upsert_inbound_sms(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Sync the inbound set to the modem's current contents, preserving our
        app-managed read state across refreshes (keyed by a stable `dedup`).
        New messages are inserted as 'unread'. Returns the list of NEW message
        rows (in arrival order) so callers can act on them (e.g. auto-reply)."""
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
            new_rows: list[dict[str, Any]] = []
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
                    new_rows.append(r)
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
        return new_rows

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

    # ── UDP listener/responder capture log ───────────────────────────────
    def add_udp_message(
        self,
        direction: str,
        port: int,
        peer: str,
        length: int,
        body: str | None = None,
        body_hex: str | None = None,
        matched_rule: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO udp_messages"
                " (ts, direction, port, peer, body, body_hex, length, matched_rule)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), direction, port, peer, body, body_hex, length, matched_rule),
            )
            self._prune("udp_messages")
            self._conn.commit()

    def recent_udp_messages(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM udp_messages ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def clear_udp_messages(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM udp_messages")
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

    # ── connectivity uptime log (one row per connected<->down edge) ──────
    def add_connectivity(self, up: bool, state: str | None, detail: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO connectivity (ts, up, state, detail) VALUES (?, ?, ?, ?)",
                (time.time(), int(up), state, detail),
            )
            self._prune("connectivity")
            self._conn.commit()

    def connectivity_last(self) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM connectivity ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def connectivity_first_ts(self) -> float | None:
        """Timestamp of the earliest connectivity record (the data horizon)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT ts FROM connectivity ORDER BY id ASC LIMIT 1"
            ).fetchone()
        return row["ts"] if row else None

    def connectivity_state_at(self, t: float) -> dict | None:
        """The connectivity edge in effect at time t (latest row at or before t)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM connectivity WHERE ts <= ? ORDER BY ts DESC, id DESC LIMIT 1",
                (t,),
            ).fetchone()
        return dict(row) if row else None

    def connectivity_between(self, t0: float, t1: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM connectivity WHERE ts >= ? AND ts <= ?"
                " ORDER BY ts ASC, id ASC",
                (t0, t1),
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

    # ── per-interface ICMP latency / packet loss ─────────────────────────
    _ICMP_SAMPLE_COLS = (
        "interface", "target", "sent", "received",
        "loss_pct", "rtt_avg_ms", "rtt_min_ms", "rtt_max_ms",
    )

    def add_icmp_samples(self, ts: float, rows: list[dict[str, Any]]) -> None:
        """Bulk-insert one probe cycle's per-(interface,target) results."""
        if not rows:
            return
        params = [
            (ts, *[r.get(c) for c in self._ICMP_SAMPLE_COLS]) for r in rows
        ]
        cols = ", ".join(self._ICMP_SAMPLE_COLS)
        with self._lock:
            self._conn.executemany(
                f"INSERT INTO icmp_samples (ts, {cols})"  # noqa: S608 — cols are internal
                f" VALUES (?, {', '.join('?' * len(self._ICMP_SAMPLE_COLS))})",
                params,
            )
            self._conn.commit()

    def icmp_samples_between(
        self, t0: float, t1: float, interface: str | None = None
    ) -> list[dict]:
        query = "SELECT * FROM icmp_samples WHERE ts >= ? AND ts <= ?"
        args: list[Any] = [t0, t1]
        if interface:
            query += " AND interface = ?"
            args.append(interface)
        query += " ORDER BY ts ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def upsert_icmp_rollups(self, period: str, rows: list[dict[str, Any]]) -> None:
        """Insert-or-replace aggregate buckets (idempotent on re-fold)."""
        if not rows:
            return
        params = [
            (
                r["bucket_start"], period, r["interface"], r["target"],
                r["sample_count"], r["sent"], r["received"], r["loss_pct"],
                r.get("rtt_avg_ms"), r.get("rtt_min_ms"), r.get("rtt_max_ms"),
            )
            for r in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO icmp_rollups"
                " (bucket_start, period, interface, target, sample_count,"
                "  sent, received, loss_pct, rtt_avg_ms, rtt_min_ms, rtt_max_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(period, bucket_start, interface, target) DO UPDATE SET"
                "  sample_count=excluded.sample_count, sent=excluded.sent,"
                "  received=excluded.received, loss_pct=excluded.loss_pct,"
                "  rtt_avg_ms=excluded.rtt_avg_ms, rtt_min_ms=excluded.rtt_min_ms,"
                "  rtt_max_ms=excluded.rtt_max_ms",
                params,
            )
            self._conn.commit()

    def icmp_rollups_between(
        self, period: str, t0: float, t1: float, interface: str | None = None
    ) -> list[dict]:
        query = (
            "SELECT * FROM icmp_rollups WHERE period = ?"
            " AND bucket_start >= ? AND bucket_start <= ?"
        )
        args: list[Any] = [period, t0, t1]
        if interface:
            query += " AND interface = ?"
            args.append(interface)
        query += " ORDER BY bucket_start ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def icmp_last_rollup_bucket(self, period: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(bucket_start) AS m FROM icmp_rollups WHERE period = ?",
                (period,),
            ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def icmp_interfaces(self, since: float = 0.0) -> list[str]:
        """Distinct interfaces seen in raw samples at/after `since`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT interface FROM icmp_samples WHERE ts >= ?"
                " ORDER BY interface",
                (since,),
            ).fetchall()
        return [r["interface"] for r in rows]

    # ── per-interface HTTP/website checks ────────────────────────────────
    _HTTP_SAMPLE_COLS = ("interface", "target", "ok", "status_code", "latency_ms")

    def add_http_samples(self, ts: float, rows: list[dict[str, Any]]) -> None:
        """Bulk-insert one HTTP-check cycle's per-(interface,target) results."""
        if not rows:
            return
        params = [(ts, *[r.get(c) for c in self._HTTP_SAMPLE_COLS]) for r in rows]
        cols = ", ".join(self._HTTP_SAMPLE_COLS)
        with self._lock:
            self._conn.executemany(
                f"INSERT INTO http_samples (ts, {cols})"  # noqa: S608 — cols are internal
                f" VALUES (?, {', '.join('?' * len(self._HTTP_SAMPLE_COLS))})",
                params,
            )
            self._conn.commit()

    def http_samples_between(
        self, t0: float, t1: float, interface: str | None = None
    ) -> list[dict]:
        query = "SELECT * FROM http_samples WHERE ts >= ? AND ts <= ?"
        args: list[Any] = [t0, t1]
        if interface:
            query += " AND interface = ?"
            args.append(interface)
        query += " ORDER BY ts ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def upsert_http_rollups(self, period: str, rows: list[dict[str, Any]]) -> None:
        """Insert-or-replace HTTP aggregate buckets (idempotent on re-fold)."""
        if not rows:
            return
        params = [
            (
                r["bucket_start"], period, r["interface"], r["target"],
                r["sample_count"], r["sent"], r["received"], r["loss_pct"],
                r.get("rtt_avg_ms"), r.get("rtt_min_ms"), r.get("rtt_max_ms"),
                r.get("status_code"),
            )
            for r in rows
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO http_rollups"
                " (bucket_start, period, interface, target, sample_count,"
                "  sent, received, loss_pct, rtt_avg_ms, rtt_min_ms, rtt_max_ms, status_code)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(period, bucket_start, interface, target) DO UPDATE SET"
                "  sample_count=excluded.sample_count, sent=excluded.sent,"
                "  received=excluded.received, loss_pct=excluded.loss_pct,"
                "  rtt_avg_ms=excluded.rtt_avg_ms, rtt_min_ms=excluded.rtt_min_ms,"
                "  rtt_max_ms=excluded.rtt_max_ms, status_code=excluded.status_code",
                params,
            )
            self._conn.commit()

    def http_rollups_between(
        self, period: str, t0: float, t1: float, interface: str | None = None
    ) -> list[dict]:
        query = (
            "SELECT * FROM http_rollups WHERE period = ?"
            " AND bucket_start >= ? AND bucket_start <= ?"
        )
        args: list[Any] = [period, t0, t1]
        if interface:
            query += " AND interface = ?"
            args.append(interface)
        query += " ORDER BY bucket_start ASC, id ASC"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [dict(r) for r in rows]

    def http_last_rollup_bucket(self, period: str) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(bucket_start) AS m FROM http_rollups WHERE period = ?",
                (period,),
            ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def prune_older_than(self, table: str, cutoff: float, ts_col: str = "ts") -> None:
        """Delete rows older than `cutoff` (epoch seconds). Time-based retention
        for high-volume tables that the fixed-row `_prune` would churn through."""
        with self._lock:
            self._conn.execute(
                f"DELETE FROM {table} WHERE {ts_col} < ?",  # noqa: S608 — internal args
                (cutoff,),
            )
            self._conn.commit()

    def _prune(self, table: str) -> None:
        # Caller holds the lock.
        self._conn.execute(
            f"DELETE FROM {table} WHERE id <= ("  # noqa: S608 — table is internal
            f"SELECT MAX(id) FROM {table}) - ?",
            (MAX_ROWS,),
        )

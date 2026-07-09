"""Traffic-audit collector thread.

Polls a flow source (conntrack, or the fake in --simulate) once a second:
flow-close events become final rows in traffic_flows, and a periodic snapshot
of the live table checkpoints long-running flows so they're visible (with
fresh counters) before they close. Read-only with respect to the network —
it never mutates modem/route state, mirroring the monitor threads.

Row lifecycle: a live flow first seen in a snapshot is inserted active=1 and
remembered by kernel flow id; later snapshots update its counters in place;
its DESTROY event finalises the same row (active=0, authoritative totals).
Flows that open and close between snapshots skip straight to a final row.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from sim_monitor.config.schema import TrafficConfig
from sim_monitor.core.events import EventLog
from sim_monitor.storage.db import Database
from sim_monitor.system.backend import BackendError
from sim_monitor.traffic.parse import FlowEvent, classify

log = logging.getLogger(__name__)

_RESTART_BACKOFF_S = 30.0
_PRUNE_INTERVAL_S = 3600.0


def effective_traffic_config(db: Database, default: TrafficConfig) -> TrafficConfig:
    """The traffic config in effect now: the UI-managed DB setting if present
    (and valid), else the config.yaml default. Read fresh each tick so UI
    edits hot-reload without restarting the thread."""
    raw = db.get_setting("traffic")
    if not raw:
        return default
    try:
        return TrafficConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - bad stored config must not stop auditing
        log.warning("invalid stored traffic config (%s); using config default", e)
        return default


class TrafficCollector:
    def __init__(
        self,
        db: Database,
        events: EventLog,
        get_config: Callable[[], TrafficConfig],
        source,
        ip_interfaces: Callable[[], dict[str, str]],
        backend_name: str = "conntrack",
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db = db
        self.events = events
        self.get_config = get_config
        self.source = source
        self.ip_interfaces = ip_interfaces
        self.backend_name = backend_name
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._local: set[str] = set()
        self._ifmap: dict[str, str] = {}  # local ip -> interface name
        self._active: dict[int, int] = {}  # kernel flow id -> traffic_flows row id
        self._started = False
        self._warned = False
        self._warnings: list[str] = []
        self._flows_recorded = 0
        self._last_snapshot: float | None = None
        self._last_prune: float | None = None
        self._last_start_attempt: float | None = None

    def run(self, stop) -> None:
        while not stop.wait(1.0):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never let the collector thread die
                log.exception("traffic collector iteration failed")
        self._stop_source()

    def tick(self) -> None:
        config = self.get_config()
        if config is None:
            return
        if not config.enabled:
            if self._started:
                self._stop_source()
                self.events.info("traffic", "traffic audit disabled")
            return
        if not self._started or not self.source.running:
            self._maybe_start()
            if not (self._started and self.source.running):
                return
        for ev in self.source.drain():
            if ev.event == "DESTROY":
                self._on_destroy(ev, config)
        now = self._monotonic()
        if (
            self._last_snapshot is None
            or now - self._last_snapshot >= config.snapshot_interval_seconds
        ):
            self._last_snapshot = now
            self._do_snapshot(config)
            self._write_status(running=True)
        if self._last_prune is None or now - self._last_prune >= _PRUNE_INTERVAL_S:
            self._last_prune = now
            self.db.prune_traffic_flows(
                self._wall_clock() - config.retention_days * 86400.0,
                config.max_flows,
            )

    # ── source lifecycle ─────────────────────────────────────────────────

    def _maybe_start(self) -> None:
        now = self._monotonic()
        if (
            self._last_start_attempt is not None
            and now - self._last_start_attempt < _RESTART_BACKOFF_S
        ):
            return
        restarting = self._last_start_attempt is not None
        self._last_start_attempt = now
        try:
            self._warnings = self.source.setup()
            self.source.start()
        except BackendError as e:
            self._started = False
            self._write_status(running=False, error=str(e))
            if not self._warned:
                self._warned = True
                self.events.error(
                    "traffic",
                    f"traffic audit unavailable: {e} "
                    "(install the 'conntrack' package / rerun install.sh)",
                )
            return
        self._started = True
        self._warned = False
        self._refresh_local()
        # Any rows still flagged active are stale checkpoints from a previous
        # run (we no longer know their kernel ids); close them so the live
        # view starts truthful.
        stale, _ = self.db.query_traffic_flows(active=True, limit=1_000_000)
        self.db.close_traffic_flows([r["id"] for r in stale])
        self._active.clear()
        self._write_status(running=True)
        if not restarting:
            msg = f"traffic audit started ({self.backend_name})"
            if self._warnings:
                msg += f" — {'; '.join(self._warnings)}"
            self.events.info("traffic", msg)
        for w in self._warnings:
            log.warning("traffic audit: %s", w)

    def _stop_source(self) -> None:
        self.source.stop()
        self._started = False
        self._last_start_attempt = None
        self.db.close_traffic_flows(list(self._active.values()))
        self._active.clear()
        self._write_status(running=False)

    def _refresh_local(self) -> None:
        ifmap = self.ip_interfaces()
        if ifmap:
            self._ifmap = ifmap
            self._local = set(ifmap)

    # ── flow bookkeeping ─────────────────────────────────────────────────

    def _row(self, ev: FlowEvent, first_seen: float, last_seen: float,
             active: bool, include_local: bool) -> dict | None:
        cls = classify(ev, self._local)
        if cls.direction == "local" and not include_local:
            return None
        return {
            "first_seen": first_seen, "last_seen": last_seen,
            "proto": ev.proto, "direction": cls.direction,
            "remote_ip": cls.remote_ip, "remote_port": cls.remote_port,
            "local_ip": cls.local_ip, "local_port": cls.local_port,
            # Which interface the flow rode, resolved from the local address at
            # capture time (cellular IPs change per session — attribution has
            # to happen now, not at query time). Unresolvable (e.g. forwarded
            # flows, where local_ip is the LAN client) stays NULL.
            "interface": self._ifmap.get(cls.local_ip),
            "bytes_sent": cls.bytes_sent, "bytes_recv": cls.bytes_recv,
            "packets_sent": cls.packets_sent, "packets_recv": cls.packets_recv,
            "active": int(active),
        }

    def _on_destroy(self, ev: FlowEvent, config: TrafficConfig) -> None:
        ts = ev.ts or self._wall_clock()
        first = ts - ev.delta_time if ev.delta_time else ts
        row = self._row(ev, first, ts, active=False,
                        include_local=config.include_local)
        row_id = self._active.pop(ev.ct_id, None) if ev.ct_id is not None else None
        if row is None:
            if row_id is not None:
                self.db.close_traffic_flows([row_id])
            return
        if row_id is not None:
            self.db.update_traffic_flow(
                row_id, ts, row["bytes_sent"], row["bytes_recv"],
                row["packets_sent"], row["packets_recv"], active=False,
            )
        else:
            self.db.add_traffic_flow(row)
        self._flows_recorded += 1

    def _do_snapshot(self, config: TrafficConfig) -> None:
        self._refresh_local()
        snap = self.source.snapshot()
        if snap is None:
            return
        now = self._wall_clock()
        seen: set[int] = set()
        for ev in snap:
            if ev.ct_id is None:
                continue
            first = now - ev.delta_time if ev.delta_time else now
            row = self._row(ev, first, now, active=True,
                            include_local=config.include_local)
            if row is None:
                continue
            seen.add(ev.ct_id)
            row_id = self._active.get(ev.ct_id)
            if row_id is not None:
                self.db.update_traffic_flow(
                    row_id, now, row["bytes_sent"], row["bytes_recv"],
                    row["packets_sent"], row["packets_recv"], active=True,
                )
            else:
                self._active[ev.ct_id] = self.db.add_traffic_flow(row)
        # Checkpointed flows gone from the table whose DESTROY we missed
        # (e.g. the event stream restarted): close them with last-known totals.
        stale = set(self._active) - seen
        if stale:
            self.db.close_traffic_flows([self._active.pop(k) for k in stale])

    def _write_status(self, running: bool, error: str | None = None) -> None:
        self.db.set_setting("traffic_status", {
            "running": running,
            "backend": self.backend_name,
            "error": error,
            "warnings": self._warnings,
            "flows_recorded": self._flows_recorded,
            "active_tracked": len(self._active),
            "updated": self._wall_clock(),
        })

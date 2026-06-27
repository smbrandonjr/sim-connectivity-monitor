"""Per-interface ICMP latency + packet-loss monitor.

Runs in its own thread. Each cycle it pings every configured target from every
up interface (cellular + any management ethernet/wifi), so the history can show
whether a problem is cellular-only or systemic. Raw samples are written to the
DB and periodically folded into hourly/daily rollups for long-term review.

Pure ping execution and parsing live in scan.engine.ping_host; pure aggregation
lives in core.latency. This module is just the scheduler + I/O glue, mirroring
HttpMonitor.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sim_monitor.config.schema import LatencyConfig
from sim_monitor.core import latency as agg
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.scan.engine import ping_host
from sim_monitor.storage.db import Database
from sim_monitor.system.netifaces import list_up_interfaces

log = logging.getLogger(__name__)

_MAX_WORKERS = 8
_ROLLUP_PERIODS = ("hour", "day")


def effective_latency_config(db: Database, default: LatencyConfig) -> LatencyConfig:
    """The latency config in effect now: the UI-managed setting stored in the DB
    if present (and valid), else the config.yaml default. Read fresh each probe
    cycle so UI edits hot-reload without restarting the thread."""
    raw = db.get_setting("latency")
    if not raw:
        return default
    try:
        return LatencyConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - bad stored config must not wedge probing
        log.warning("invalid stored latency config (%s); using config default", e)
        return default


class PingMonitor:
    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], LatencyConfig | None],
        trigger: threading.Event | None = None,
        pinger: Callable[..., dict] = ping_host,
        list_interfaces: Callable[[], list[str]] = list_up_interfaces,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self.trigger = trigger or threading.Event()
        self.pinger = pinger
        self.list_interfaces = list_interfaces
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        # Last probe time (monotonic); due is derived from the current interval so
        # shortening it takes effect immediately.
        self._last_sent: float | None = None

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            forced = self.trigger.wait(timeout=1.0)
            if stop.is_set():
                return
            if forced:
                self.trigger.clear()
            try:
                self._iteration(forced)
            except Exception:  # noqa: BLE001 - never let the monitor thread die
                log.exception("ping monitor iteration failed")

    def _iteration(self, forced: bool) -> None:
        config = self.get_config()
        if config is None or not config.targets:
            return
        now = self._monotonic()
        due = self._last_sent is None or now - self._last_sent >= config.interval_seconds
        if not (forced or (config.enabled and due)):
            return
        self._last_sent = now
        self.probe(config)

    def _interfaces(self, config: LatencyConfig) -> list[str]:
        names = config.interfaces or self.list_interfaces()
        # Always include the live cellular interface (it's the whole point), even
        # if enumeration missed it mid-activation.
        cell = self.store.get().interface
        if cell and cell not in names:
            names = [*names, cell]
        excluded = set(config.exclude_interfaces)
        return [n for n in names if n not in excluded]

    def probe(self, config: LatencyConfig) -> list[dict]:
        """Run the interface×target matrix once, store samples + fold rollups."""
        interfaces = self._interfaces(config)
        if not interfaces:
            return []
        jobs = [(iface, target) for iface in interfaces for target in config.targets]
        cell = self.store.get().interface
        # Remember the cellular interface so heartbeat placeholders can still
        # resolve its recent stats while momentarily disconnected (degraded).
        if cell and self.db.get_setting("cellular_interface") != cell:
            self.db.set_setting("cellular_interface", cell)

        def _one(job: tuple[str, str]) -> dict:
            iface, target = job
            res = self.pinger(
                target, interface=iface,
                count=config.packet_count, timeout=config.timeout_seconds,
            )
            return {
                "interface": iface, "target": target,
                "sent": res.get("sent", 0), "received": res.get("received", 0),
                "loss_pct": res.get("loss_pct", 100.0),
                "rtt_avg_ms": res.get("avg_ms"),
                "rtt_min_ms": res.get("min_ms"),
                "rtt_max_ms": res.get("max_ms"),
            }

        workers = min(_MAX_WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(_one, jobs))

        ts = self._wall_clock()
        self.db.add_icmp_samples(ts, rows)

        # Flag any interface that's totally dark to all targets — a useful
        # breadcrumb in the event log (cellular down, or eth unplugged).
        for iface in interfaces:
            iface_rows = [r for r in rows if r["interface"] == iface]
            if iface_rows and all(r["received"] == 0 for r in iface_rows):
                tag = " (cellular)" if iface == cell else ""
                self.events.warning(
                    "latency", f"{iface}{tag}: 100% packet loss to all targets"
                )

        self._fold_rollups(ts)
        self._prune(config, ts)
        return rows

    def _fold_rollups(self, now: float) -> None:
        for period in _ROLLUP_PERIODS:
            last = self.db.icmp_last_rollup_bucket(period) or 0.0
            cur = agg.bucket_start(now, period)
            samples = self.db.icmp_samples_between(last, cur)
            size = agg.period_seconds(period)
            rollups = [
                r for r in agg.bucket(samples, period)
                if r["bucket_start"] + size <= now  # only fully-complete buckets
            ]
            self.db.upsert_icmp_rollups(period, rollups)

    def _prune(self, config: LatencyConfig, now: float) -> None:
        self.db.prune_older_than(
            "icmp_samples", now - config.raw_retention_days * agg.DAY
        )
        self.db.prune_older_than(
            "icmp_rollups", now - config.rollup_retention_days * agg.DAY,
            ts_col="bucket_start",
        )


# ── simulate-mode fake pinger ────────────────────────────────────────────────

# Plausible per-interface baselines (ms) for the dev/simulate UI.
_FAKE_BASELINES = {"wwan0": 48.0, "eth0": 7.0, "wlan0": 16.0}
SIMULATE_INTERFACES = ["wwan0", "eth0", "wlan0"]


def make_fake_pinger(rng=None) -> Callable[..., dict]:
    """A `ping_host`-shaped callable producing plausible jittered latency so the
    whole feature can be exercised on a dev box with no real network."""
    import random

    rand = rng or random.Random()

    def fake_ping(host: str, interface: str | None = None,
                  count: int = 5, timeout: int = 2) -> dict:
        base = _FAKE_BASELINES.get(interface or "", 25.0)
        # cellular jitters more; rare loss on cellular/wifi.
        jitter = base * (0.25 if interface == "wwan0" else 0.12)
        loss_chance = 0.04 if interface == "wwan0" else 0.01
        received = sum(1 for _ in range(count) if rand.random() > loss_chance)
        if received == 0:
            return {"sent": count, "received": 0, "loss_pct": 100.0,
                    "avg_ms": None, "min_ms": None, "max_ms": None}
        samples = [max(1.0, rand.gauss(base, jitter)) for _ in range(received)]
        loss = round((1 - received / count) * 100, 1)
        return {
            "sent": count, "received": received, "loss_pct": loss,
            "avg_ms": round(sum(samples) / len(samples), 2),
            "min_ms": round(min(samples), 2),
            "max_ms": round(max(samples), 2),
        }

    return fake_ping

"""Per-interface HTTP/website reachability monitor.

A sibling of PingMonitor: each cycle it GETs every configured URL from every up
interface (bound to that interface), records the status + request latency, and
folds raw samples into hourly/daily rollups. Storage is separate from the ICMP
samples (http_samples/http_rollups) so ping and web reachability stay isolated.

Pure aggregation lives in core.latency; the HTTP request itself is scan.engine.
http_probe. This module is just the scheduler + I/O glue, mirroring PingMonitor.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from sim_monitor.config.schema import HttpCheckConfig
from sim_monitor.core import latency as agg
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.scan.engine import http_probe
from sim_monitor.storage.db import Database
from sim_monitor.system.netifaces import list_up_interfaces

log = logging.getLogger(__name__)

_MAX_WORKERS = 8
_ROLLUP_PERIODS = ("hour", "day")


def effective_http_check_config(db: Database, default: HttpCheckConfig) -> HttpCheckConfig:
    """The HTTP-check config in effect now: the UI-managed setting stored in the
    DB if present (and valid), else the config.yaml default. Read fresh each
    cycle so UI edits hot-reload without restarting the thread."""
    raw = db.get_setting("http_checks")
    if not raw:
        return default
    try:
        return HttpCheckConfig.model_validate(raw)
    except Exception as e:  # noqa: BLE001 - bad stored config must not wedge probing
        log.warning("invalid stored http-check config (%s); using config default", e)
        return default


class HttpCheckMonitor:
    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], HttpCheckConfig | None],
        trigger: threading.Event | None = None,
        prober: Callable[..., dict] = http_probe,
        list_interfaces: Callable[[], list[str]] = list_up_interfaces,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self.trigger = trigger or threading.Event()
        self.prober = prober
        self.list_interfaces = list_interfaces
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._next_due: float | None = None

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
                log.exception("http-check monitor iteration failed")

    def _iteration(self, forced: bool) -> None:
        config = self.get_config()
        if config is None or not config.targets:
            return
        now = self._monotonic()
        due = self._next_due is None or now >= self._next_due
        if not (forced or (config.enabled and due)):
            return
        self._next_due = now + config.interval_seconds
        self.probe(config)

    def _interfaces(self, config: HttpCheckConfig) -> list[str]:
        names = config.interfaces or self.list_interfaces()
        cell = self.store.get().interface
        if cell and cell not in names:
            names = [*names, cell]
        excluded = set(config.exclude_interfaces)
        return [n for n in names if n not in excluded]

    def probe(self, config: HttpCheckConfig) -> list[dict]:
        """Run the interface×URL matrix once, store samples + fold rollups."""
        interfaces = self._interfaces(config)
        if not interfaces:
            return []
        jobs = [(iface, url) for iface in interfaces for url in config.targets]
        cell = self.store.get().interface
        # Remember the cellular interface so heartbeat placeholders can resolve
        # its recent stats even while momentarily disconnected (degraded).
        if cell and self.db.get_setting("cellular_interface") != cell:
            self.db.set_setting("cellular_interface", cell)

        def _one(job: tuple[str, str]) -> dict:
            iface, url = job
            res = self.prober(url, interface=iface, timeout=config.timeout_seconds)
            status = res.get("status")
            ok = bool(res.get("ok")) and status is not None and status < 400
            return {
                "interface": iface, "target": url,
                "ok": 1 if ok else 0,
                "status_code": status,
                "latency_ms": res.get("latency_ms") if ok else None,
            }

        workers = min(_MAX_WORKERS, len(jobs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rows = list(pool.map(_one, jobs))

        ts = self._wall_clock()
        self.db.add_http_samples(ts, rows)

        # Flag any interface where every check failed — a useful breadcrumb.
        for iface in interfaces:
            iface_rows = [r for r in rows if r["interface"] == iface]
            if iface_rows and all(r["ok"] == 0 for r in iface_rows):
                tag = " (cellular)" if iface == cell else ""
                self.events.warning(
                    "http_check", f"{iface}{tag}: all web checks failed"
                )

        self._fold_rollups(ts)
        self._prune(config, ts)
        return rows

    def _fold_rollups(self, now: float) -> None:
        for period in _ROLLUP_PERIODS:
            last = self.db.http_last_rollup_bucket(period) or 0.0
            cur = agg.bucket_start(now, period)
            samples = self.db.http_samples_between(last, cur)
            size = agg.period_seconds(period)
            rollups = [
                r for r in agg.bucket_http(samples, period)
                if r["bucket_start"] + size <= now  # only fully-complete buckets
            ]
            self.db.upsert_http_rollups(period, rollups)

    def _prune(self, config: HttpCheckConfig, now: float) -> None:
        self.db.prune_older_than(
            "http_samples", now - config.raw_retention_days * agg.DAY
        )
        self.db.prune_older_than(
            "http_rollups", now - config.rollup_retention_days * agg.DAY,
            ts_col="bucket_start",
        )


# ── simulate-mode fake HTTP prober ───────────────────────────────────────────

# Plausible per-interface HTTP latencies (ms) — higher than ICMP (TLS + request).
_FAKE_HTTP_BASELINES = {"wwan0": 180.0, "eth0": 35.0, "wlan0": 70.0}


def make_fake_http_prober(rng=None) -> Callable[..., dict]:
    """An `http_probe`-shaped callable for simulate mode, so the web-check
    feature can be exercised on a dev box with no real network."""
    import random

    rand = rng or random.Random()

    def fake_http(url: str, interface: str | None = None, timeout: float = 10) -> dict:
        base = _FAKE_HTTP_BASELINES.get(interface or "", 90.0)
        jitter = base * (0.3 if interface == "wwan0" else 0.15)
        fail_chance = 0.05 if interface == "wwan0" else 0.01
        if rand.random() < fail_chance:
            # Half the failures are a server error, half a timeout/conn error.
            if rand.random() < 0.5:
                return {"ok": True, "status": 503,
                        "latency_ms": round(max(1.0, rand.gauss(base, jitter)))}
            return {"ok": False, "status": None,
                    "latency_ms": round(timeout * 1000), "error": "timeout"}
        return {"ok": True, "status": 204,
                "latency_ms": round(max(1.0, rand.gauss(base, jitter)))}

    return fake_http

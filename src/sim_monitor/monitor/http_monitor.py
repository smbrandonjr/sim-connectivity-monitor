"""Periodic HTTP heartbeat with SIM-specific placeholder substitution.

Runs in its own thread; reads daemon status from the StateStore, never touches
the modem. The config it uses is resolved by the daemon (the UI-managed global
config, or a profile's monitor block when that profile overrides it). Probes
fire on schedule while CONNECTED, keep firing over any interface while degraded
(if configured), and on an explicit RunMonitorNow trigger.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

import requests

from sim_monitor.config.schema import MonitorConfig
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor.placeholders import render, render_body_fields
from sim_monitor.monitor.schedule import is_active
from sim_monitor.monitor.transport import make_session
from sim_monitor.storage.db import Database
from sim_monitor.system.host import collect_host_metrics, collect_interface_ips
from sim_monitor.system.netifaces import list_up_interfaces

log = logging.getLogger(__name__)

PUBLIC_IP_URL = "https://api.ipify.org"
PUBLIC_IP_INTERVAL = 300  # seconds


def resolve_egress(
    config: MonitorConfig, snapshot, list_interfaces: Callable[[], list[str]] = list_up_interfaces
) -> str | None:
    """The interface to bind the heartbeat socket to (None = OS routing). Falls
    back to OS routing when the requested interface isn't currently up, so a
    missing Wi-Fi link (say) doesn't silently drop every heartbeat. Shared by the
    monitor (at send time) and the API (for the {egress_interface} preview)."""
    if config.egress == "cellular":
        return snapshot.interface if snapshot.state is State.CONNECTED else None
    if config.egress == "wlan":
        return next(
            (n for n in list_interfaces() if n.startswith(("wlan", "wlp"))), None
        )
    return None  # "auto" — let the OS pick the route


def latency_placeholder_context(db: Database, interface: str | None) -> dict:
    """Cellular latency/loss heartbeat placeholders (latency_ms/loss_pct +
    1h/3h/6h/24h windows) for the given interface, from the last 24h of raw
    ICMP samples. Empty/None values when the latency monitor has no data.

    Falls back to the last-known cellular interface (recorded by the ping
    monitor) so a degraded heartbeat still carries the cellular path's recent
    stats even though the live interface is momentarily None."""
    from sim_monitor.core.latency import payload_stats

    iface = interface or db.get_setting("cellular_interface")
    now = time.time()
    samples = (
        db.icmp_samples_between(now - 86400, now, interface=iface)
        if iface else []
    )
    return payload_stats(samples, now)


def http_check_placeholder_context(db: Database, interface: str | None) -> dict:
    """Web-check (HTTP) heartbeat placeholders (http_latency_ms/http_loss_pct +
    1h/3h/6h/24h windows) for the given interface, from the last 24h of HTTP
    samples. Mirrors latency_placeholder_context but keyed with an `http_`
    prefix so ping and web stats stay distinct in the payload. Empty/None values
    when the web-check monitor has no data."""
    from sim_monitor.core.latency import http_sample_to_metric, payload_stats

    iface = interface or db.get_setting("cellular_interface")
    now = time.time()
    rows = (
        db.http_samples_between(now - 86400, now, interface=iface)
        if iface else []
    )
    metrics = [http_sample_to_metric(r) for r in rows]
    return payload_stats(metrics, now, prefix="http_")


class HttpMonitor:
    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], MonitorConfig | None],
        trigger: threading.Event,
        wall_clock: Callable[[], datetime] | None = None,
        list_interfaces: Callable[[], list[str]] = list_up_interfaces,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self.trigger = trigger
        # Wall-clock source for schedule windows (injectable for tests); the
        # monotonic clock still drives interval pacing.
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        # Enumerates up interfaces, to resolve the configured egress (injectable).
        self.list_interfaces = list_interfaces
        # Per-destination last-sent time (monotonic), keyed by a stable dest key.
        # Due is derived as now - last >= interval, so editing the interval takes
        # effect immediately instead of waiting out the previously-scheduled due.
        self._last_sent: dict[str, float] = {}
        self._next_public_ip = 0.0

    @staticmethod
    def _dest_key(dest) -> str:
        return f"{dest.egress}|{dest.method}|{dest.url}"

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            forced = self.trigger.wait(timeout=1.0)
            if stop.is_set():
                return
            if forced:
                self.trigger.clear()
            self._iteration(forced)
            self._maybe_public_ip()

    def _maybe_public_ip(self) -> None:
        """Resolve the cellular public IP periodically (bound to the cellular
        interface so it's the SIM's address, not the LAN's)."""
        if time.monotonic() < self._next_public_ip:
            return
        snapshot = self.store.get()
        if snapshot.state is not State.CONNECTED:
            return
        self._next_public_ip = time.monotonic() + PUBLIC_IP_INTERVAL
        try:
            resp = make_session(snapshot.interface).get(PUBLIC_IP_URL, timeout=10)
            ip = resp.text.strip()
            if resp.ok and ip and len(ip) <= 45:
                self.store.update(public_ip=ip)
        except requests.RequestException as e:
            log.debug("public IP lookup failed: %s", e)

    def _iteration(self, forced: bool) -> None:
        config = self.get_config()
        if config is None or not config.destinations:
            if forced:
                self.db.add_monitor_result(
                    url="", status_code=None, latency_ms=None, ok=False,
                    error="no heartbeat endpoint configured",
                )
            return
        snapshot = self.store.get()
        if snapshot.monitor_paused and not forced:
            return  # paused: hold the schedule; resumes where it left off
        connected = snapshot.state is State.CONNECTED
        if not connected and not forced and not config.send_when_degraded:
            return  # don't reschedule; fire as soon as we're connected again
        now = time.monotonic()
        wall = self._wall_clock()
        # Decide which destinations are due this tick (each on its own interval +
        # schedule window), then send the shared payload to each.
        to_fire = []
        for dest in config.destinations:
            if not dest.enabled:
                continue
            key = self._dest_key(dest)
            last = self._last_sent.get(key)
            scheduled = (
                config.enabled
                and is_active(dest.schedule, wall)
                and (last is None or now - last >= dest.interval_seconds)
            )
            if forced or scheduled:
                self._last_sent[key] = now
                to_fire.append(dest)
        if not to_fire:
            return
        context = self._build_context(snapshot)
        for dest in to_fire:
            self.probe(config, dest, snapshot, context)

    def _build_context(self, snapshot) -> dict:
        """The shared placeholder context for one cycle (everything except the
        per-destination egress_interface, which probe() stamps in)."""
        context = snapshot.placeholder_context()
        context.update(collect_host_metrics())
        context.update(collect_interface_ips())
        context.update(latency_placeholder_context(self.db, snapshot.interface))
        context.update(http_check_placeholder_context(self.db, snapshot.interface))
        context["sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return context

    def probe(self, config: MonitorConfig, dest, snapshot=None, context=None) -> bool:
        """Send one heartbeat to one destination and record the result.

        The payload (config.body / config.body_fields) is shared across all
        destinations; only the URL/method/headers and the egress interface differ
        per destination. The socket binds to the destination's egress (Wi-Fi by
        default, cellular, or unbound). {status} still reflects cellular health
        regardless of which path the report travels over.
        """
        snapshot = snapshot if snapshot is not None else self.store.get()
        context = dict(context) if context is not None else self._build_context(snapshot)
        bind_interface = resolve_egress(dest, snapshot, self.list_interfaces)
        # The interface this heartbeat actually bound to (None = OS-routed); lets
        # the endpoint record which path each heartbeat travelled.
        context["egress_interface"] = bind_interface
        # URL + headers are string-templated; the body is either built from
        # structured fields (always-valid JSON) or string-templated for raw use.
        url, _u1 = render(dest.url, context)
        headers, unknown = {}, set()
        for k, v in dest.headers.items():
            headers[k], u = render(v, context)
            unknown |= u
        if config.body_fields:
            body = render_body_fields(config.body_fields, context)
            headers.setdefault("Content-Type", "application/json")
        else:
            body, u = render(config.body, context)
            unknown |= u
        unknown |= _u1
        if unknown:
            self.events.warning(
                "monitor", f"unknown placeholders left intact: {sorted(unknown)}"
            )
        started = time.monotonic()
        try:
            response = make_session(bind_interface).request(
                dest.method,
                url,
                headers=headers,
                data=body.encode("utf-8") if body else None,
                timeout=dest.timeout_seconds,
            )
        except requests.RequestException as e:
            latency = (time.monotonic() - started) * 1000
            self.db.add_monitor_result(
                url, None, latency, ok=False, error=str(e), interface=bind_interface
            )
            self.events.warning("monitor", f"probe failed ({url}): {e}")
            return False
        latency = (time.monotonic() - started) * 1000
        ok = response.status_code in dest.expect_status
        error = None if ok else f"unexpected status {response.status_code}"
        self.db.add_monitor_result(
            url, response.status_code, latency, ok=ok, error=error, interface=bind_interface
        )
        if ok:
            log.info("monitor probe ok: %s %s (%.0f ms)", response.status_code, url, latency)
        else:
            self.events.warning("monitor", f"probe returned {response.status_code} for {url}")
        return ok

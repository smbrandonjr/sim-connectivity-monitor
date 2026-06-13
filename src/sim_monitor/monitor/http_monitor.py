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

import requests

from sim_monitor.config.schema import MonitorConfig
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor.placeholders import render, render_body_fields
from sim_monitor.monitor.transport import make_session
from sim_monitor.storage.db import Database
from sim_monitor.system.host import collect_host_metrics

log = logging.getLogger(__name__)


class HttpMonitor:
    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_config: Callable[[], MonitorConfig | None],
        trigger: threading.Event,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_config = get_config
        self.trigger = trigger
        self._next_due: float | None = None

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            forced = self.trigger.wait(timeout=1.0)
            if stop.is_set():
                return
            if forced:
                self.trigger.clear()
            self._iteration(forced)

    def _iteration(self, forced: bool) -> None:
        config = self.get_config()
        if config is None or config.request is None:
            if forced:
                self.db.add_monitor_result(
                    url="", status_code=None, latency_ms=None, ok=False,
                    error="no heartbeat endpoint configured",
                )
            return
        now = time.monotonic()
        scheduled = config.enabled and (self._next_due is None or now >= self._next_due)
        if not (forced or scheduled):
            return
        snapshot = self.store.get()
        if snapshot.monitor_paused and not forced:
            return  # paused: hold the schedule; resumes where it left off
        connected = snapshot.state is State.CONNECTED
        if not connected and not forced and not config.send_when_degraded:
            return  # don't reschedule; fire as soon as we're connected again
        self._next_due = now + config.interval_seconds
        self.probe(config)

    def probe(self, config: MonitorConfig) -> bool:
        """Send one heartbeat and record the result. Returns success.

        While CONNECTED the socket is bound to the cellular interface (a
        success proves cellular egress). Otherwise the request goes out
        unbound — over ethernet/wifi if available — carrying
        {status}=degraded so the endpoint learns *why* instead of silence.
        """
        request = config.request
        assert request is not None
        snapshot = self.store.get()
        bind_interface = (
            snapshot.interface
            if snapshot.state is State.CONNECTED and config.bind_cellular
            else None
        )
        context = snapshot.placeholder_context()
        context.update(collect_host_metrics())
        context["sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # URL + headers are string-templated; the body is either built from
        # structured fields (always-valid JSON) or string-templated for raw use.
        url, _u1 = render(request.url, context)
        headers, unknown = {}, set()
        for k, v in request.headers.items():
            headers[k], u = render(v, context)
            unknown |= u
        if request.body_fields:
            body = render_body_fields(request.body_fields, context)
            headers.setdefault("Content-Type", "application/json")
        else:
            body, u = render(request.body, context)
            unknown |= u
        unknown |= _u1
        if unknown:
            self.events.warning(
                "monitor", f"unknown placeholders left intact: {sorted(unknown)}"
            )
        started = time.monotonic()
        try:
            response = make_session(bind_interface).request(
                request.method,
                url,
                headers=headers,
                data=body.encode("utf-8") if body else None,
                timeout=request.timeout_seconds,
            )
        except requests.RequestException as e:
            latency = (time.monotonic() - started) * 1000
            self.db.add_monitor_result(url, None, latency, ok=False, error=str(e))
            self.events.warning("monitor", f"probe failed: {e}")
            return False
        latency = (time.monotonic() - started) * 1000
        ok = response.status_code in request.expect_status
        error = None if ok else f"unexpected status {response.status_code}"
        self.db.add_monitor_result(url, response.status_code, latency, ok=ok, error=error)
        if ok:
            log.info("monitor probe ok: %s %s (%.0f ms)", response.status_code, url, latency)
        else:
            self.events.warning("monitor", f"probe returned {response.status_code} for {url}")
        return ok

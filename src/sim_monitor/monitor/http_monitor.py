"""Periodic HTTP heartbeat with SIM-specific placeholder substitution.

Runs in its own thread; reads daemon status from the StateStore, never touches
the modem. Probes fire only while CONNECTED (scheduled) or on explicit
RunMonitorNow trigger from the UI.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import requests

from sim_monitor.config.schema import Profile
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor.placeholders import render_request
from sim_monitor.monitor.transport import make_session
from sim_monitor.storage.db import Database

log = logging.getLogger(__name__)


class HttpMonitor:
    def __init__(
        self,
        store: StateStore,
        db: Database,
        events: EventLog,
        get_profile: Callable[[], Profile | None],
        trigger: threading.Event,
    ) -> None:
        self.store = store
        self.db = db
        self.events = events
        self.get_profile = get_profile
        self.trigger = trigger
        self._next_due: float | None = None

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            forced = self.trigger.wait(timeout=1.0)
            if stop.is_set():
                return
            if forced:
                self.trigger.clear()
            profile = self.get_profile()
            if profile is None or profile.monitor.request is None:
                if forced:
                    self.db.add_monitor_result(
                        url="", status_code=None, latency_ms=None, ok=False,
                        error="no monitor request configured for the active profile",
                    )
                continue
            now = time.monotonic()
            scheduled = (
                profile.monitor.enabled
                and (self._next_due is None or now >= self._next_due)
            )
            if not (forced or scheduled):
                continue
            snapshot = self.store.get()
            connected = snapshot.state is State.CONNECTED
            if not connected and not forced and not profile.monitor.send_when_degraded:
                continue  # don't reschedule; fire as soon as we're connected again
            self._next_due = now + profile.monitor.interval_seconds
            self.probe(profile)

    def probe(self, profile: Profile) -> bool:
        """Send one monitor request and record the result. Returns success.

        While CONNECTED the socket is bound to the cellular interface (a
        success proves cellular egress). Otherwise the request goes out
        unbound — over ethernet/wifi if available — carrying
        {status}=degraded so the endpoint learns *why* instead of silence.
        """
        request = profile.monitor.request
        assert request is not None
        snapshot = self.store.get()
        bind_interface = (
            snapshot.interface if snapshot.state is State.CONNECTED else None
        )
        url, headers, body, unknown = render_request(
            request.url, request.headers, request.body, snapshot.placeholder_context()
        )
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

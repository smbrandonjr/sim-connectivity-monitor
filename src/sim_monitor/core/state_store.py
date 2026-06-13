"""Thread-safe snapshot of live daemon status, read by the web UI and monitor.

The daemon is the only writer; readers get an immutable copy so they can never
observe a half-updated state.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any

from sim_monitor.core.states import State

# Concise per-state explanations for {status_message}.
_STATE_MESSAGES = {
    State.NO_MODEM: "no modem detected",
    State.MODEM_FOUND: "modem found, waiting for SIM",
    State.SIM_READY: "selecting profile",
    State.CONFIGURING: "configuring modem",
    State.CONNECTING: "connecting",
    State.DEGRADED: "recovery in progress",
}


def _short(text: str, limit: int = 120) -> str:
    """First line only, capped — heartbeat payloads should stay readable."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[: limit - 3] + "..." if len(line) > limit else line


def derive_status(snapshot: Snapshot) -> tuple[str, str]:
    """({status}, {status_message}) for heartbeat payloads.

    connected     -> cellular is up
    fallback_test -> radio intentionally off (don't page yourself over it)
    degraded      -> the Pi is alive but cellular is down; message says why
    """
    if snapshot.state is State.CONNECTED:
        if snapshot.operator:
            return "connected", f"cellular connected via {snapshot.operator}"
        return "connected", "cellular connected"
    if snapshot.state is State.FALLBACK_TEST:
        return "fallback_test", "fallback test in progress (radio off)"
    base = _STATE_MESSAGES.get(snapshot.state, "cellular down")
    if snapshot.last_error:
        return "degraded", f"{base}: {_short(snapshot.last_error)}"
    return "degraded", base


@dataclass(frozen=True)
class DiagnosticEntry:
    command: str
    output: str
    ok: bool


@dataclass(frozen=True)
class DiagnosticsReport:
    ran_at: float
    entries: tuple[DiagnosticEntry, ...] = ()
    note: str = ""  # e.g. "no modem detected" when commands could not run


@dataclass(frozen=True)
class FallbackStatus:
    active: bool = False
    until: float | None = None  # epoch seconds when airplane mode ends
    iccid_before: str | None = None


@dataclass(frozen=True)
class Snapshot:
    state: State = State.NO_MODEM
    state_since: float = field(default_factory=time.time)
    vendor: str | None = None
    model: str | None = None
    imei: str | None = None
    sim_present: bool = False
    iccid: str | None = None
    imsi: str | None = None
    operator: str | None = None
    signal_rssi: int | None = None
    signal_percent: int | None = None
    interface: str | None = None
    ip_address: str | None = None
    routing_ok: bool | None = None
    active_profile: str | None = None
    forced_profile: str | None = None
    profile_count: int = 0
    last_error: str | None = None
    fallback: FallbackStatus = field(default_factory=FallbackStatus)
    diagnostics: DiagnosticsReport | None = None
    monitor_paused: bool = False  # runtime-only; resets on service restart
    updated_at: float = field(default_factory=time.time)

    def placeholder_context(self) -> dict[str, Any]:
        """Values available to monitor request templates."""
        import socket

        status, status_message = derive_status(self)
        return {
            "status": status,
            "status_message": status_message,
            "iccid": self.iccid,
            "imei": self.imei,
            "imsi": self.imsi,
            "operator": self.operator,
            "signal_rssi": self.signal_rssi,
            "signal_percent": self.signal_percent,
            "ip_address": self.ip_address,
            "interface": self.interface,
            "hostname": socket.gethostname(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "state": self.state.value,
            "profile_name": self.active_profile,
        }


class StateStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()

    def get(self) -> Snapshot:
        with self._lock:
            return self._snapshot

    def update(self, **fields: Any) -> Snapshot:
        with self._lock:
            self._snapshot = replace(self._snapshot, updated_at=time.time(), **fields)
            return self._snapshot

    def set_state(self, state: State, **fields: Any) -> Snapshot:
        with self._lock:
            if state != self._snapshot.state:
                fields["state_since"] = time.time()
            self._snapshot = replace(
                self._snapshot, state=state, updated_at=time.time(), **fields
            )
            return self._snapshot

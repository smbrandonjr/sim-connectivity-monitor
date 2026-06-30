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
class SerialPort:
    """A serial port as shown in the UI's modem-setup view."""

    device: str
    vid: int | None = None
    pid: int | None = None
    interface: int | None = None
    mm_claimed: bool = False     # ModemManager listed this port
    is_current: bool = False     # sim-monitor is using this port now
    tested: bool = False         # an AT probe has been run against it
    responded: bool = False      # it answered the AT probe
    identity: str | None = None  # e.g. "SIMCOM SIM7080" (from the probe)
    detail: str | None = None    # probe error / note


@dataclass(frozen=True)
class ModemSetup:
    """State for the guided modem / AT-port setup UI."""

    at_port: str = "auto"            # the configured value ("auto" or a device path)
    modem_present: bool = False      # ModemManager sees a modem
    scanned_at: float | None = None  # epoch of the last port scan
    ports: tuple[SerialPort, ...] = ()


@dataclass(frozen=True)
class Snapshot:
    state: State = State.NO_MODEM
    state_since: float = field(default_factory=time.time)
    vendor: str | None = None
    model: str | None = None
    firmware: str | None = None
    imei: str | None = None
    sim_present: bool = False
    iccid: str | None = None
    imsi: str | None = None
    sim_name: str | None = None  # user label for the current SIM (by ICCID)
    operator: str | None = None
    registration: str | None = None
    signal_rssi: int | None = None
    signal_percent: int | None = None
    interface: str | None = None
    ip_address: str | None = None
    gateway: str | None = None
    public_ip: str | None = None
    apn: str | None = None
    routing_ok: bool | None = None
    active_profile: str | None = None
    forced_profile: str | None = None
    profile_count: int = 0
    last_error: str | None = None
    fallback: FallbackStatus = field(default_factory=FallbackStatus)
    fallback_armed: bool = False  # run a fallback test on the next SIM attach (one-shot)
    diagnostics: DiagnosticsReport | None = None
    modem_setup: ModemSetup = field(default_factory=ModemSetup)
    rat_supported: tuple[str, ...] = ()  # RATs the current modem can be forced to
    monitor_paused: bool = False  # runtime-only; resets on service restart
    sms_unread: int = 0
    telemetry: dict = field(default_factory=dict)  # latest deep link metrics
    updated_at: float = field(default_factory=time.time)

    def placeholder_context(self) -> dict[str, Any]:
        """Native-typed values available to heartbeat fields/templates. Numbers
        stay numbers, strings stay strings; unknowns are None (omitted by the
        structured body builder). Host metrics (uptime/cpu/mem/temp) and
        sampled_at are merged in by the monitor at send time."""
        import socket

        status, status_message = derive_status(self)
        t = self.telemetry or {}
        ctx: dict[str, Any] = {
            "status": status,
            "status_message": status_message,
            "iccid": self.iccid,
            "imei": self.imei,
            "imsi": self.imsi,
            "operator": self.operator,
            "registration": self.registration,
            "signal_rssi": self.signal_rssi,
            "rssi": self.signal_rssi,          # alias
            "signal_percent": self.signal_percent,
            "ip_address": self.ip_address,
            "gateway": self.gateway,
            "public_ip": self.public_ip,
            "interface": self.interface,
            "apn": self.apn,
            "hostname": socket.gethostname(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "state": self.state.value,
            "profile_name": self.active_profile,
            "sim_name": self.sim_name,
            "firmware": self.firmware,
            "vendor": self.vendor,
            "model": self.model,
            "modem_model": (f"{self.vendor} {self.model}".strip() or None)
            if (self.vendor or self.model) else None,
            "last_error": self.last_error,
        }
        # Deep telemetry (native types), key names matching the receiving schema.
        for key in ("rat", "rsrp", "rsrq", "sinr", "band", "earfcn", "cell_id",
                    "tac", "pci", "mcc", "mnc", "operator_numeric", "channel"):
            ctx[key] = t.get(key)
        return ctx


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

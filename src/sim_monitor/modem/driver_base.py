"""Vendor-neutral modem driver interface.

The daemon talks to modems only through this ABC. Real drivers (Quectel,
SIMCOM, Telit — Phase 3) implement it with AT commands over a dedicated serial
port; FakeModemDriver implements it in memory for tests and --simulate.

Any method may raise ModemError (port gone, command failed, timeout); the
daemon treats that as a recoverable failure, never a crash.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_parser import ActualPdpContext, SignalQuality


class ModemError(Exception):
    pass


@dataclass(frozen=True)
class ModemIdentity:
    vendor: str
    model: str
    imei: str


@dataclass(frozen=True)
class SimStatus:
    present: bool
    iccid: str | None = None
    imsi: str | None = None
    detail: str | None = None  # human-readable reason when not usable (e.g. "SIM PIN required")


@dataclass(frozen=True)
class UrcEvent:
    """A classified unsolicited result code from the modem.

    kind ∈ {new_sms, sms_deliver, sim_status, registration, nitz, ring,
    no_carrier, unknown}; `fields` carries the parsed details; `raw` is the
    original line (always recorded for forensics)."""

    raw: str
    kind: str
    fields: dict


class ModemDriver(ABC):
    """One instance per detected modem."""

    name: str = "base"

    # Read-only commands for the web UI's Diagnostics page; vendor drivers
    # extend with their quirk queries.
    DIAGNOSTIC_COMMANDS: list[str] = [
        "AT+CSQ",
        "AT+CREG?",
        "AT+CEREG?",
        "AT+COPS?",
        "AT+CPIN?",
        "AT+CGDCONT?",
    ]

    @abstractmethod
    def execute_raw(self, command: str, timeout: float | None = None) -> list[str]:
        """Run one raw AT command and return its payload lines."""

    @abstractmethod
    def get_identity(self) -> ModemIdentity: ...

    @abstractmethod
    def get_sim_status(self) -> SimStatus: ...

    @abstractmethod
    def get_operator(self) -> str | None: ...

    @abstractmethod
    def get_signal(self) -> SignalQuality | None: ...

    @abstractmethod
    def get_pdp_contexts(self) -> list[ActualPdpContext]: ...

    @abstractmethod
    def define_pdp_context(self, context: PdpContext) -> None: ...

    @abstractmethod
    def delete_pdp_context(self, cid: int) -> None: ...

    @abstractmethod
    def set_airplane(self, on: bool) -> None: ...

    @abstractmethod
    def clear_forbidden_plmn(self) -> None:
        """Wipe the SIM's forbidden-PLMN list (EF_FPLMN).

        Repeated attach rejects (e.g. retry storms, out-of-plan carriers) get
        in-plan networks blacklisted ON THE SIM, persisting across reboots and
        power cycles. Clearing it lets the modem retry every carrier."""

    @abstractmethod
    def full_reset(self) -> None:
        """Vendor-specific full modem reboot (AT+CFUN=1,1 / AT+CRESET / AT#REBOOT)."""

    @abstractmethod
    def run_init_commands(self, commands: list[str]) -> None: ...

    @abstractmethod
    def enable_event_reporting(self) -> None:
        """Turn on verbose URCs (new-SMS, SIM status, registration, NITZ).

        Best-effort: unsupported commands are ignored so one missing quirk
        never blocks bring-up."""

    @abstractmethod
    def poll_events(self) -> list[UrcEvent]:
        """Return (and clear) URCs captured since the last call."""


class ModemDetector(ABC):
    """Finds an attached modem and returns a ready driver, or None."""

    @abstractmethod
    def detect(self) -> ModemDriver | None: ...

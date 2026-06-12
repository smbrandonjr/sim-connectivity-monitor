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


class ModemDriver(ABC):
    """One instance per detected modem."""

    name: str = "base"

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
    def full_reset(self) -> None:
        """Vendor-specific full modem reboot (AT+CFUN=1,1 / AT+CRESET / AT#REBOOT)."""

    @abstractmethod
    def run_init_commands(self, commands: list[str]) -> None: ...


class ModemDetector(ABC):
    """Finds an attached modem and returns a ready driver, or None."""

    @abstractmethod
    def detect(self) -> ModemDriver | None: ...

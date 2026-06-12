"""Scriptable in-memory modem for tests and --simulate mode.

Tests and the simulation harness mutate the public attributes directly to
script scenarios: SIM removal/swap, stray firmware PDP contexts, airplane
mode side effects (the Hologram fallback applet switching profiles), and
command failures.
"""

from __future__ import annotations

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_parser import ActualPdpContext, SignalQuality
from sim_monitor.modem.driver_base import (
    ModemDetector,
    ModemDriver,
    ModemError,
    ModemIdentity,
    SimStatus,
)

DEFAULT_ICCID = "8944500612345678901"
FALLBACK_ICCID = "8944999900000000001"


class FakeModemDriver(ModemDriver):
    name = "fake"

    def __init__(self, iccid: str = DEFAULT_ICCID) -> None:
        self.identity = ModemIdentity(vendor="FakeCorp", model="FM100", imei="490154203237518")
        self.sim_present = True
        self.iccid = iccid
        self.imsi = "234500000000001"
        self.operator: str | None = "Hologram"
        self.signal = SignalQuality(rssi_dbm=-77, percent=58)
        self.airplane = False
        # Simulates firmware that boots with auto-created contexts.
        self.contexts: dict[int, ActualPdpContext] = {
            1: ActualPdpContext(cid=1, pdp_type="IPv4", apn="internet"),
            8: ActualPdpContext(cid=8, pdp_type="IPv4v6", apn="ims"),
        }
        # Scripting hooks
        self.fail_all = False  # every call raises ModemError (port wedged)
        self.fallback_iccid: str | None = None  # applied when airplane mode ends
        self.at_log: list[str] = []  # records init commands and resets

    def _check(self) -> None:
        if self.fail_all:
            raise ModemError("simulated modem failure")

    def get_identity(self) -> ModemIdentity:
        self._check()
        return self.identity

    def get_sim_status(self) -> SimStatus:
        self._check()
        if not self.sim_present:
            return SimStatus(present=False)
        return SimStatus(present=True, iccid=self.iccid, imsi=self.imsi)

    def get_operator(self) -> str | None:
        self._check()
        return None if self.airplane else self.operator

    def get_signal(self) -> SignalQuality | None:
        self._check()
        return None if self.airplane else self.signal

    def get_pdp_contexts(self) -> list[ActualPdpContext]:
        self._check()
        return [self.contexts[cid] for cid in sorted(self.contexts)]

    def define_pdp_context(self, context: PdpContext) -> None:
        self._check()
        self.contexts[context.cid] = ActualPdpContext(
            cid=context.cid, pdp_type=context.pdp_type, apn=context.apn
        )

    def delete_pdp_context(self, cid: int) -> None:
        self._check()
        self.contexts.pop(cid, None)

    def set_airplane(self, on: bool) -> None:
        self._check()
        leaving = self.airplane and not on
        self.airplane = on
        if leaving and self.fallback_iccid:
            # The SIM applet switched profiles while radio was off.
            self.iccid = self.fallback_iccid
            self.imsi = "234509999999999"
            self.fallback_iccid = None

    def full_reset(self) -> None:
        self._check()
        self.at_log.append("RESET")
        self.airplane = False

    def clear_forbidden_plmn(self) -> None:
        self._check()
        self.at_log.append("CLEAR_FPLMN")

    def run_init_commands(self, commands: list[str]) -> None:
        self._check()
        self.at_log.extend(commands)

    def execute_raw(self, command: str, timeout: float | None = None) -> list[str]:
        self._check()
        self.at_log.append(command)
        canned = {
            "AT+CSQ": ["+CSQ: 18,99"],
            "AT+CREG?": ["+CREG: 0,5"],
            "AT+CEREG?": ["+CEREG: 0,5"],
            "AT+COPS?": ['+COPS: 0,0,"Hologram",7'],
            "AT+CPIN?": ["+CPIN: READY"],
            "AT+CGDCONT?": [
                f'+CGDCONT: {c.cid},"{c.pdp_type}","{c.apn}"'
                for c in self.get_pdp_contexts()
            ],
        }
        return canned.get(command, [f"{command}: simulated OK"])


class FakeDetector(ModemDetector):
    """Returns the fake driver, optionally only after N detect() calls."""

    def __init__(self, driver: FakeModemDriver | None, appear_after: int = 0) -> None:
        self.driver = driver
        self.appear_after = appear_after
        self.calls = 0

    def detect(self) -> ModemDriver | None:
        self.calls += 1
        if self.driver is None or self.calls <= self.appear_after:
            return None
        return self.driver

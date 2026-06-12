"""ModemDriver implementation over an AT channel, with 3GPP-standard defaults.

Vendor subclasses (drivers/) override only the quirks: ICCID command, full
reset command, and PDP auth syntax. Anything raising ATCommandError/ModemError
propagates as ModemError, which the daemon treats as recoverable.
"""

from __future__ import annotations

import logging

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem import at_parser
from sim_monitor.modem.at_channel import ATChannel, ATCommandError
from sim_monitor.modem.at_parser import ActualPdpContext, ATParseError, SignalQuality
from sim_monitor.modem.driver_base import (
    ModemDriver,
    ModemError,
    ModemIdentity,
    SimStatus,
)

log = logging.getLogger(__name__)

# +CME ERROR codes (and message fragments) that mean "no SIM", not "modem broken".
_SIM_ABSENT_MARKERS = ("ERROR: 10", "ERROR: 13", "ERROR: 14", "SIM NOT INSERTED", "SIM FAILURE")

_AUTH_CODES = {"none": 0, "pap": 1, "chap": 2}


class ATModemDriver(ModemDriver):
    name = "generic-3gpp"
    VENDOR_IDS: frozenset[int] = frozenset()
    ICCID_COMMAND = "AT+CCID"
    RESET_COMMAND = "AT+CFUN=1,1"

    def __init__(self, channel: ATChannel) -> None:
        self.at = channel

    def _parse(self, parser, lines):
        try:
            return parser(lines)
        except ATParseError as e:
            raise ModemError(str(e)) from e

    def get_identity(self) -> ModemIdentity:
        vendor = self._parse(at_parser.parse_cgmi, self.at.execute("AT+CGMI"))
        model = self._parse(at_parser.parse_cgmi, self.at.execute("AT+CGMM"))
        imei = self._parse(at_parser.parse_imei, self.at.execute("AT+CGSN"))
        return ModemIdentity(vendor=vendor, model=model, imei=imei)

    def get_sim_status(self) -> SimStatus:
        try:
            cpin = self._parse(at_parser.parse_cpin, self.at.execute("AT+CPIN?"))
        except ATCommandError as e:
            if any(marker in str(e).upper() for marker in _SIM_ABSENT_MARKERS):
                return SimStatus(present=False, detail="no SIM inserted")
            raise
        if cpin != "READY":
            return SimStatus(present=False, detail=f"SIM locked ({cpin}); PINs are unsupported")
        iccid = self._parse(at_parser.parse_iccid, self.at.execute(self.ICCID_COMMAND))
        imsi = self._parse(at_parser.parse_imsi, self.at.execute("AT+CIMI"))
        return SimStatus(present=True, iccid=iccid, imsi=imsi)

    def get_operator(self) -> str | None:
        return self._parse(at_parser.parse_cops, self.at.execute("AT+COPS?"))

    def get_signal(self) -> SignalQuality | None:
        return self._parse(at_parser.parse_csq, self.at.execute("AT+CSQ"))

    def get_pdp_contexts(self) -> list[ActualPdpContext]:
        try:
            return at_parser.parse_cgdcont(self.at.execute("AT+CGDCONT?"))
        except ATCommandError:
            # Some firmware errors when zero contexts are defined.
            return []

    def define_pdp_context(self, context: PdpContext) -> None:
        pdp_type = at_parser.pdp_type_to_at(context.pdp_type)
        self.at.execute(f'AT+CGDCONT={context.cid},"{pdp_type}","{context.apn}"')
        if context.auth != "none":
            self._set_auth(context)

    def _set_auth(self, context: PdpContext) -> None:
        # 3GPP TS 27.007 AT+CGAUTH; Telit overrides with AT#PDPAUTH.
        self.at.execute(
            f'AT+CGAUTH={context.cid},{_AUTH_CODES[context.auth]},'
            f'"{context.username}","{context.password}"'
        )

    def delete_pdp_context(self, cid: int) -> None:
        self.at.execute(f"AT+CGDCONT={cid}")

    def set_airplane(self, on: bool) -> None:
        self.at.execute("AT+CFUN=4" if on else "AT+CFUN=1", timeout=30)

    def full_reset(self) -> None:
        try:
            self.at.execute(self.RESET_COMMAND, timeout=10)
        except ModemError:
            # The port often dies mid-reply during a reset; that's the point.
            pass
        finally:
            self.at.close()  # device node will re-enumerate

    def run_init_commands(self, commands: list[str]) -> None:
        for command in commands:
            self.at.execute(command, timeout=15)

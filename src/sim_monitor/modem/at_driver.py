"""ModemDriver implementation over an AT channel, with 3GPP-standard defaults.

Vendor subclasses (drivers/) override only the quirks: ICCID command, full
reset command, and PDP auth syntax. Anything raising ATCommandError/ModemError
propagates as ModemError, which the daemon treats as recoverable.
"""

from __future__ import annotations

import logging

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem import at_parser, pdu
from sim_monitor.modem.at_channel import ATChannel, ATCommandError
from sim_monitor.modem.at_parser import ActualPdpContext, ATParseError, SignalQuality
from sim_monitor.modem.driver_base import (
    ModemDriver,
    ModemError,
    ModemIdentity,
    RawSms,
    SimStatus,
    UrcEvent,
)

log = logging.getLogger(__name__)

# +CME ERROR codes (and message fragments) that mean "no SIM", not "modem broken".
_SIM_ABSENT_MARKERS = ("ERROR: 10", "ERROR: 13", "ERROR: 14", "SIM NOT INSERTED", "SIM FAILURE")

_AUTH_CODES = {"none": 0, "pap": 1, "chap": 2}


class ATModemDriver(ModemDriver):
    name = "generic-3gpp"
    VENDOR_IDS: frozenset[int] = frozenset()
    ICCID_COMMAND = "AT+CCID"
    # Tried in order after ICCID_COMMAND if it errors — modem families within a
    # vendor differ (e.g. SIM7600 uses AT+CICCID, SIM707x uses AT+CCID).
    ICCID_FALLBACK_COMMANDS: tuple[str, ...] = ()
    RESET_COMMAND = "AT+CFUN=1,1"

    # rat-identifier -> AT command(s) that force it. 3GPP TS 27.007 AT+WS46
    # covers the universal RATs; vendor subclasses override with richer sets
    # (LTE-M/NB-IoT/5G). 25 = all 3GPP, 12 = GSM, 22 = UTRAN, 28 = E-UTRAN.
    RAT_COMMANDS: dict[str, list[str]] = {
        "auto": ["AT+WS46=25"],
        "lte": ["AT+WS46=28"],
        "3g": ["AT+WS46=22"],
        "2g": ["AT+WS46=12"],
    }

    # Best-effort URC enablers; vendor subclasses append quirks. Each is sent
    # tolerantly — a modem that rejects one still gets the rest.
    EVENT_REPORTING_COMMANDS = (
        "AT+CMEE=2",          # verbose error text
        "AT+CREG=2",          # registration URCs with location
        "AT+CGREG=2",
        "AT+CEREG=2",
        "AT+CTZR=1",          # network time-zone (NITZ) URCs
        'AT+CNMI=2,1,0,0,0',  # new SMS -> +CMTI indication (store, don't dump)
    )

    def __init__(self, channel: ATChannel) -> None:
        self.at = channel
        self._urc_lines: list[str] = []
        channel.set_urc_handler(self._urc_lines.append)

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

    def get_firmware(self) -> str:
        return self._parse(at_parser.parse_cgmi, self.at.execute("AT+CGMR"))

    def get_sim_status(self) -> SimStatus:
        try:
            cpin = self._parse(at_parser.parse_cpin, self.at.execute("AT+CPIN?"))
        except ATCommandError as e:
            if any(marker in str(e).upper() for marker in _SIM_ABSENT_MARKERS):
                return SimStatus(present=False, detail="no SIM inserted")
            raise
        if cpin != "READY":
            return SimStatus(present=False, detail=f"SIM locked ({cpin}); PINs are unsupported")
        iccid = self._read_iccid()
        imsi = self._parse(at_parser.parse_imsi, self.at.execute("AT+CIMI"))
        return SimStatus(present=True, iccid=iccid, imsi=imsi)

    def _read_iccid(self) -> str:
        """Read the ICCID, trying the driver's primary command then any fallbacks
        so one driver covers a vendor's differing modem families."""
        last_error: ModemError | None = None
        for command in (self.ICCID_COMMAND, *self.ICCID_FALLBACK_COMMANDS):
            try:
                return self._parse(at_parser.parse_iccid, self.at.execute(command))
            except ModemError as e:  # ATCommandError (modem ERROR) or parse failure
                last_error = e
        raise last_error or ModemError("no ICCID command supported by this modem")

    def get_operator(self) -> str | None:
        return self._parse(at_parser.parse_cops, self.at.execute("AT+COPS?"))

    def get_signal(self) -> SignalQuality | None:
        return self._parse(at_parser.parse_csq, self.at.execute("AT+CSQ"))

    def get_telemetry(self) -> dict:
        # Generic 3GPP: only CSQ is universally available.
        sq = self.get_signal()
        data: dict = {}
        if sq:
            data["rssi"] = sq.rssi_dbm
            data["signal_percent"] = sq.percent
        return data

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

    def supported_rats(self) -> list[str]:
        return list(self.RAT_COMMANDS.keys())

    def set_rat(self, rat: str) -> None:
        commands = self.RAT_COMMANDS.get(rat)
        if commands is None:
            raise ModemError(f"this modem ({self.name}) cannot force RAT {rat!r}")
        for command in commands:
            self.at.execute(command, timeout=15)

    def reprobe_sim(self) -> None:
        # Minimal-functionality cycle: re-initializes the SIM session so a newly
        # inserted card is detected without a full modem reboot.
        self.at.execute("AT+CFUN=0", timeout=15)
        self.at.execute("AT+CFUN=1", timeout=30)

    def clear_forbidden_plmn(self) -> None:
        # CRSM UPDATE BINARY on EF_FPLMN (28539): four empty 3-byte entries.
        self.at.execute('AT+CRSM=214,28539,0,0,12,"FFFFFFFFFFFFFFFFFFFFFFFF"', timeout=10)

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

    def execute_raw(self, command: str, timeout: float | None = None) -> list[str]:
        return self.at.execute(command, timeout=timeout or 10)

    def enable_event_reporting(self) -> None:
        for command in self.EVENT_REPORTING_COMMANDS:
            try:
                self.at.execute(command, timeout=5)
            except ModemError as e:
                log.debug("event-reporting command %s not supported: %s", command, e)

    def poll_events(self) -> list[UrcEvent]:
        try:
            self.at.drain_urcs()  # pull any URCs buffered during idle gaps
        except ModemError as e:
            log.debug("URC drain failed: %s", e)
        if not self._urc_lines:
            return []
        lines, self._urc_lines = self._urc_lines, []
        events = []
        for line in lines:
            kind, fields = at_parser.classify_urc(line)
            events.append(UrcEvent(raw=line, kind=kind, fields=fields))
        return events

    # ── SMS (PDU mode) ──────────────────────────────────────────────────────
    def list_sms(self) -> list[RawSms]:
        self.at.execute("AT+CMGF=0")  # PDU mode
        lines = self.at.execute("AT+CMGL=4", timeout=15)  # 4 = ALL messages
        return [RawSms(idx, stat, pdu_hex) for idx, stat, pdu_hex in at_parser.parse_cmgl(lines)]

    def send_sms(self, number: str, text: str) -> int:
        self.at.execute("AT+CMGF=0")
        parts = pdu.encode_submit(number, text)
        for pdu_hex, tpdu_len in parts:
            self.at.send_with_prompt(f"AT+CMGS={tpdu_len}", pdu_hex, timeout=30)
        return len(parts)

    def delete_sms(self, index: int) -> None:
        self.at.execute(f"AT+CMGD={index}")

    def delete_all_sms(self) -> None:
        self.at.execute("AT+CMGD=1,4")  # delete all messages in all states

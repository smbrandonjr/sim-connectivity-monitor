"""Quectel (EC25, EC21, EG25-G, BG96, RM5xx) — USB VID 0x2c7c."""

from __future__ import annotations

from sim_monitor.modem import at_parser
from sim_monitor.modem.at_driver import ATModemDriver
from sim_monitor.modem.driver_base import ModemError


class QuectelDriver(ATModemDriver):
    name = "quectel"
    VENDOR_IDS = frozenset({0x2C7C})
    ICCID_COMMAND = "AT+QCCID"
    RESET_COMMAND = "AT+CFUN=1,1"
    # Quectel SIM insertion/refresh URCs (+QSIMSTAT) are the key OTA signal.
    EVENT_REPORTING_COMMANDS = (
        *ATModemDriver.EVENT_REPORTING_COMMANDS,
        "AT+QSIMSTAT=1",          # report SIM (un)insertion / refresh as +QSIMSTAT
        'AT+QINDCFG="all",1',     # enable all URC indications
    )

    def get_telemetry(self) -> dict:
        """Rich LTE metrics: QCSQ (RSRP/RSRQ/SINR), serving cell, network info."""
        data: dict = {}
        for command, parser, keys in (
            ("AT+QCSQ", at_parser.parse_qcsq, ("rssi", "rsrp", "rsrq", "sinr", "rat")),
            ('AT+QENG="servingcell"', at_parser.parse_qeng_servingcell,
             ("cell_id", "pci", "earfcn", "band", "tac", "mcc", "mnc",
              "rsrp", "rsrq", "sinr", "rssi")),
            ("AT+QNWINFO", at_parser.parse_qnwinfo, ("operator_numeric", "channel")),
        ):
            try:
                parsed = parser(self.at.execute(command))
            except ModemError:
                parsed = None  # best-effort: a missing query never breaks telemetry
            if parsed:
                for k in keys:
                    if parsed.get(k) is not None:
                        data[k] = parsed[k]
        return data
    DIAGNOSTIC_COMMANDS = [
        *ATModemDriver.DIAGNOSTIC_COMMANDS,
        "AT+QNWINFO",                 # serving network: RAT, operator, band
        'AT+QCFG="roamservice"',      # 2 = roaming enabled (Hologram is ALWAYS roaming)
        'AT+QCFG="nwscanmode"',       # 0 = RAT auto
        'AT+QCFG="nwscanseq"',        # scan order
        'AT+QCFG="band"',             # band mask
        "AT+QCCID",
    ]

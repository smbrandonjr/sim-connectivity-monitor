"""Quectel (EC25, EC21, EG25-G, BG96, RM5xx) — USB VID 0x2c7c."""

from __future__ import annotations

from sim_monitor.modem.at_driver import ATModemDriver


class QuectelDriver(ATModemDriver):
    name = "quectel"
    VENDOR_IDS = frozenset({0x2C7C})
    ICCID_COMMAND = "AT+QCCID"
    RESET_COMMAND = "AT+CFUN=1,1"
    DIAGNOSTIC_COMMANDS = [
        *ATModemDriver.DIAGNOSTIC_COMMANDS,
        "AT+QNWINFO",                 # serving network: RAT, operator, band
        'AT+QCFG="roamservice"',      # 2 = roaming enabled (Hologram is ALWAYS roaming)
        'AT+QCFG="nwscanmode"',       # 0 = RAT auto
        'AT+QCFG="nwscanseq"',        # scan order
        'AT+QCFG="band"',             # band mask
        "AT+QCCID",
    ]

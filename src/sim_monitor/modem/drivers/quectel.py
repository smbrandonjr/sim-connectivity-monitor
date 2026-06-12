"""Quectel (EC25, EC21, EG25-G, BG96, RM5xx) — USB VID 0x2c7c."""

from __future__ import annotations

from sim_monitor.modem.at_driver import ATModemDriver


class QuectelDriver(ATModemDriver):
    name = "quectel"
    VENDOR_IDS = frozenset({0x2C7C})
    ICCID_COMMAND = "AT+QCCID"
    RESET_COMMAND = "AT+CFUN=1,1"

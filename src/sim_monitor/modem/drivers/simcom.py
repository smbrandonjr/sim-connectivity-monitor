"""SIMCOM (SIM7600, SIM7000, A76xx) — USB VID 0x1e0e."""

from __future__ import annotations

from sim_monitor.modem.at_driver import ATModemDriver


class SimcomDriver(ATModemDriver):
    name = "simcom"
    VENDOR_IDS = frozenset({0x1E0E})
    ICCID_COMMAND = "AT+CICCID"  # answers +ICCID: <number>
    RESET_COMMAND = "AT+CRESET"
    DIAGNOSTIC_COMMANDS = [
        *ATModemDriver.DIAGNOSTIC_COMMANDS,
        "AT+CPSI?",   # serving cell info
        "AT+CICCID",
    ]

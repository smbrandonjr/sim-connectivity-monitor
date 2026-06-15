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

    # CNMP network-mode preference: 2 auto, 13 GSM, 14 WCDMA, 38 LTE. CMNB
    # (SIM7000/7070 Cat-M/NB): 1 Cat-M, 2 NB-IoT. One driver covers very different
    # SIMCOM models (SIM7600 does 3G; the LPWA SIM707x does Cat-M/NB/2G but not
    # 3G/5G), so a mode the specific module lacks is rejected at the modem and
    # surfaced as a clear event. 5G (SIM82xx) is intentionally not mapped here.
    RAT_COMMANDS = {
        "auto": ["AT+CNMP=2"],
        "2g": ["AT+CNMP=13"],
        "3g": ["AT+CNMP=14"],
        "lte": ["AT+CNMP=38"],
        "lte_m": ["AT+CNMP=38", "AT+CMNB=1"],
        "nb_iot": ["AT+CNMP=38", "AT+CMNB=2"],
    }

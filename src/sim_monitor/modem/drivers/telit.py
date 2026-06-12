"""Telit (LE910, ME910, FN980) — USB VID 0x1bc7."""

from __future__ import annotations

from sim_monitor.config.schema import PdpContext
from sim_monitor.modem.at_driver import _AUTH_CODES, ATModemDriver


class TelitDriver(ATModemDriver):
    name = "telit"
    VENDOR_IDS = frozenset({0x1BC7})
    ICCID_COMMAND = "AT#CCID"
    RESET_COMMAND = "AT#REBOOT"
    DIAGNOSTIC_COMMANDS = [
        *ATModemDriver.DIAGNOSTIC_COMMANDS,
        "AT#RFSTS",   # serving cell / RF status
        "AT#CCID",
    ]

    def _set_auth(self, context: PdpContext) -> None:
        self.at.execute(
            f'AT#PDPAUTH={context.cid},{_AUTH_CODES[context.auth]},'
            f'"{context.username}","{context.password}"'
        )

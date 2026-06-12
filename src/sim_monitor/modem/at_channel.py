"""Serial AT command transport over the modem's dedicated AT port.

The port is hidden from ModemManager by udev (ID_MM_PORT_IGNORE), so we own it
exclusively. All access is serialized by a lock; every command is bounded by a
deadline so a mute modem can never hang the daemon. On errors the port is
closed and transparently reopened on the next command (USB resets make the
device node vanish and reappear).
"""

from __future__ import annotations

import logging
import threading
import time

import serial

from sim_monitor.modem.driver_base import ModemError

log = logging.getLogger(__name__)

FINAL_OK = ("OK",)
FINAL_ERROR_PREFIXES = ("ERROR", "+CME ERROR", "+CMS ERROR")


class ATCommandError(ModemError):
    """The modem answered with ERROR / +CME ERROR / +CMS ERROR."""


class ATChannel:
    def __init__(
        self,
        port: str,
        baud: int = 115200,
        timeout: float = 5.0,
        serial_factory=serial.Serial,
    ) -> None:
        self.port = port
        self.baud = baud
        self.default_timeout = timeout
        self._serial_factory = serial_factory
        self._ser = None
        self._lock = threading.RLock()

    def open(self) -> None:
        with self._lock:
            if self._ser is not None:
                return
            try:
                # Short read timeout: execute() loops readline() against its
                # own deadline, so commands stay responsive to it.
                self._ser = self._serial_factory(self.port, self.baud, timeout=0.5)
            except (serial.SerialException, OSError) as e:
                raise ModemError(f"cannot open AT port {self.port}: {e}") from e

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def execute(self, command: str, timeout: float | None = None) -> list[str]:
        """Send one AT command, return payload lines (final OK stripped).

        Raises ATCommandError on a modem error result, ModemError on
        transport problems or timeout.
        """
        deadline = time.monotonic() + (timeout or self.default_timeout)
        with self._lock:
            self.open()
            assert self._ser is not None
            try:
                self._ser.reset_input_buffer()
                self._ser.write((command + "\r").encode("ascii"))
                return self._read_response(command, deadline)
            except (serial.SerialException, OSError) as e:
                self.close()
                raise ModemError(f"AT port I/O error on {self.port}: {e}") from e

    def _read_response(self, command: str, deadline: float) -> list[str]:
        payload: list[str] = []
        while True:
            if time.monotonic() > deadline:
                self.close()
                raise ModemError(f"timeout waiting for response to {command!r}")
            raw = self._ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line or line == command:  # echo
                continue
            if line in FINAL_OK:
                return payload
            if line.startswith(FINAL_ERROR_PREFIXES):
                raise ATCommandError(f"{command!r} -> {line}")
            payload.append(line)

"""Serial AT command transport over the modem's dedicated AT port.

The port is hidden from ModemManager by udev (ID_MM_PORT_IGNORE), so we own it
exclusively. All access is serialized by a lock; every command is bounded by a
deadline so a mute modem can never hang the daemon. On errors the port is
closed and transparently reopened on the next command (USB resets make the
device node vanish and reappear).

**URC capture.** Modems emit unsolicited result codes (URCs) — new-SMS
indications (`+CMTI`), SIM refresh/insert status, registration changes, NITZ —
asynchronously, between commands. We do NOT flush them away (the old code
called `reset_input_buffer()` before every command and was blind to all of
this). Instead:
  - before sending a command we *drain* whatever unsolicited lines are buffered
    and hand them to the URC handler;
  - while reading a command's reply we divert clearly-asynchronous URCs to the
    handler and keep collecting the actual response;
  - the daemon calls `drain_urcs()` once per tick to pick up URCs that arrived
    during idle gaps.
No background reader thread: the daemon owns this port single-threaded and
polls frequently, so per-tick draining captures URCs within a tick with far
less complexity (and no lock fights over the serial handle).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import serial

from sim_monitor.modem.driver_base import ModemError

log = logging.getLogger(__name__)

FINAL_OK = ("OK",)
FINAL_ERROR_PREFIXES = ("ERROR", "+CME ERROR", "+CMS ERROR")

# Prefixes that are ALWAYS unsolicited — safe to divert even mid-command.
# (Registration prefixes like +CEREG are deliberately NOT here: they are also
# solicited replies to AT+CEREG?, so during a command we collect them as the
# response and only treat them as URCs when draining idle gaps.)
_ALWAYS_ASYNC = (
    "+CMTI:", "+CMT:", "+CDS:", "+CBM:", "+CMGR:",
    "+QIND:", "+QSIMSTAT:", "+QUSIM:",
    "+CTZV:", "+CTZE:", "+CTZDST:", "*PSUTTZ", "+PACSP",
    "RING", "+CRING:", "+CLIP:", "NO CARRIER", "NO DIALTONE", "+CGEV:",
)


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
        self._urc_handler: Callable[[str], None] | None = None

    def set_urc_handler(self, handler: Callable[[str], None] | None) -> None:
        """Register a callback invoked with each raw unsolicited line."""
        self._urc_handler = handler

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
                self._drain_pending()  # capture URCs before clobbering the buffer
                self._ser.write((command + "\r").encode("ascii"))
                return self._read_response(command, deadline)
            except (serial.SerialException, OSError) as e:
                self.close()
                raise ModemError(f"AT port I/O error on {self.port}: {e}") from e

    def send_with_prompt(
        self, command: str, payload: str, timeout: float = 30.0
    ) -> list[str]:
        """Two-stage AT command: send `command`, wait for the '>' prompt, then
        send `payload` + Ctrl-Z and read the final result. Used by AT+CMGS."""
        deadline = time.monotonic() + timeout
        with self._lock:
            self.open()
            assert self._ser is not None
            try:
                self._drain_pending()
                self._ser.write((command + "\r").encode("ascii"))
                self._wait_for_prompt(deadline)
                self._ser.write(payload.encode("ascii") + b"\x1a")  # Ctrl-Z submits
                return self._read_response(command, deadline)
            except (serial.SerialException, OSError) as e:
                self.close()
                raise ModemError(f"AT port I/O error on {self.port}: {e}") from e

    def _wait_for_prompt(self, deadline: float) -> None:
        buf = b""
        while time.monotonic() <= deadline:
            ch = self._ser.read(1)
            if not ch:
                continue
            buf += ch
            if b">" in buf:
                return
            if b"ERROR" in buf:
                raise ATCommandError(f"prompt request failed: {buf.decode(errors='replace')}")
        self.close()
        raise ModemError("timeout waiting for '>' SMS prompt")

    def drain_urcs(self) -> None:
        """Dispatch any buffered unsolicited lines (called once per daemon tick)."""
        with self._lock:
            if self._ser is None:
                return
            try:
                self._drain_pending()
            except (serial.SerialException, OSError) as e:
                self.close()
                raise ModemError(f"AT port I/O error on {self.port}: {e}") from e

    # ------------------------------------------------------------- internals

    def _has_data(self) -> bool:
        try:
            return self._ser.in_waiting > 0
        except (AttributeError, OSError, serial.SerialException):
            return False

    def _drain_pending(self) -> None:
        # We are between commands, so every complete buffered line is unsolicited.
        guard = 200  # never spin forever on a chatty modem
        while self._has_data() and guard > 0:
            guard -= 1
            raw = self._ser.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                self._dispatch_urc(line)

    def _dispatch_urc(self, line: str) -> None:
        if self._urc_handler is not None:
            try:
                self._urc_handler(line)
            except Exception:
                log.exception("URC handler raised for line %r", line)

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
            if line.startswith(_ALWAYS_ASYNC):
                self._dispatch_urc(line)  # interleaved URC, not our reply
                continue
            payload.append(line)

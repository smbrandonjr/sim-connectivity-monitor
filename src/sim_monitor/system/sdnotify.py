"""Minimal sd_notify client (systemd Type=notify + WatchdogSec support).

No-ops when NOTIFY_SOCKET is absent (dev machines, plain `python -m` runs).
"""

from __future__ import annotations

import os
import socket


class SdNotifier:
    def __init__(self) -> None:
        self._addr = os.environ.get("NOTIFY_SOCKET")
        if self._addr and self._addr.startswith("@"):
            self._addr = "\0" + self._addr[1:]  # abstract socket namespace
        self._sock: socket.socket | None = None
        if self._addr:
            self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)

    def _send(self, payload: str) -> None:
        if self._sock and self._addr:
            try:
                self._sock.sendto(payload.encode(), self._addr)
            except OSError:
                pass  # notification loss must never hurt the daemon

    def ready(self) -> None:
        self._send("READY=1")

    def watchdog(self) -> None:
        self._send("WATCHDOG=1")

    def status(self, text: str) -> None:
        self._send(f"STATUS={text}")

"""HTTP transport for the monitor.

On Linux the session's sockets are bound to the cellular interface with
SO_BINDTODEVICE (requires root), so a successful probe proves traffic really
egressed over cellular — even if the routing table would prefer ethernet.
Elsewhere (Windows dev / simulate mode) it falls back to a plain session.
"""

from __future__ import annotations

import socket
import sys

import requests
from requests.adapters import HTTPAdapter

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)  # Linux constant


class BindToDeviceAdapter(HTTPAdapter):
    def __init__(self, interface: str, **kwargs) -> None:
        self._socket_options = [
            (socket.SOL_SOCKET, SO_BINDTODEVICE, interface.encode() + b"\0")
        ]
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["socket_options"] = self._socket_options
        return super().init_poolmanager(*args, **kwargs)


def make_session(interface: str | None = None) -> requests.Session:
    session = requests.Session()
    if interface and sys.platform.startswith("linux"):
        adapter = BindToDeviceAdapter(interface)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
    return session

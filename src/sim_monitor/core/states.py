from __future__ import annotations

from enum import StrEnum


class State(StrEnum):
    NO_MODEM = "NO_MODEM"
    MODEM_FOUND = "MODEM_FOUND"
    SIM_READY = "SIM_READY"
    CONFIGURING = "CONFIGURING"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DEGRADED = "DEGRADED"
    FALLBACK_TEST = "FALLBACK_TEST"

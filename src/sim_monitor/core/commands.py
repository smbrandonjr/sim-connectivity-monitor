"""Commands the web UI (or CLI) sends to the daemon via a queue.

The daemon drains the queue at the start of every tick; handlers run inside
the daemon thread, so they may touch the modem/backend freely.
"""

from __future__ import annotations

import queue
from dataclasses import dataclass


@dataclass(frozen=True)
class Reconnect:
    pass


@dataclass(frozen=True)
class ResetModem:
    pass


@dataclass(frozen=True)
class ForceProfile:
    name: str


@dataclass(frozen=True)
class ReleaseForce:
    pass


@dataclass(frozen=True)
class StartFallbackTest:
    duration_seconds: int | None = None  # None = use profile's fallback_test setting


@dataclass(frozen=True)
class AbortFallbackTest:
    pass


@dataclass(frozen=True)
class RunMonitorNow:
    pass


@dataclass(frozen=True)
class RunDiagnostics:
    commands: tuple[str, ...] = ()  # empty = the driver's standard bundle


@dataclass(frozen=True)
class PauseMonitor:
    pass


@dataclass(frozen=True)
class ResumeMonitor:
    pass


@dataclass(frozen=True)
class SendSms:
    number: str
    text: str


@dataclass(frozen=True)
class DeleteSms:
    row_id: int


@dataclass(frozen=True)
class ClearSms:
    pass


@dataclass(frozen=True)
class RefreshSms:
    pass


@dataclass(frozen=True)
class MarkSmsRead:
    pass


@dataclass(frozen=True)
class SetSimName:
    name: str


@dataclass(frozen=True)
class ReloadMonitorConfig:
    pass


@dataclass(frozen=True)
class ReloadProfiles:
    pass


@dataclass(frozen=True)
class ScanSerialPorts:
    pass


@dataclass(frozen=True)
class ProbeAtPort:
    device: str


@dataclass(frozen=True)
class SetAtPort:
    device: str  # "" or "auto" -> automatic detection


Command = (
    Reconnect
    | ResetModem
    | ForceProfile
    | ReleaseForce
    | StartFallbackTest
    | AbortFallbackTest
    | RunMonitorNow
    | RunDiagnostics
    | PauseMonitor
    | ResumeMonitor
    | SendSms
    | DeleteSms
    | ClearSms
    | RefreshSms
    | MarkSmsRead
    | SetSimName
    | ReloadMonitorConfig
    | ReloadProfiles
    | ScanSerialPorts
    | ProbeAtPort
    | SetAtPort
)


class CommandQueue:
    def __init__(self) -> None:
        self._q: queue.Queue[Command] = queue.Queue()

    def put(self, command: Command) -> None:
        self._q.put(command)

    def drain(self) -> list[Command]:
        commands = []
        while True:
            try:
                commands.append(self._q.get_nowait())
            except queue.Empty:
                return commands

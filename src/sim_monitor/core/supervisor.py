"""Failure recovery policy: exponential backoff up an escalation ladder.

Each consecutive failure escalates one rung and doubles the wait (capped).
After the last rung the supervisor parks: it keeps retrying the gentlest
action at a fixed long interval forever. It never asks for a process exit or
a Pi reboot — connectivity problems are a state, not a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RecoveryAction(StrEnum):
    RECONNECT = "reconnect"  # nmcli connection up retry
    MODEM_DISABLE_ENABLE = "modem_disable_enable"  # mmcli --disable/--enable
    AT_RESET = "at_reset"  # vendor full reset via AT
    USB_POWER_CYCLE = "usb_power_cycle"  # sysfs power cycle


LADDER = [
    RecoveryAction.RECONNECT,
    RecoveryAction.MODEM_DISABLE_ENABLE,
    RecoveryAction.AT_RESET,
    RecoveryAction.USB_POWER_CYCLE,
]


@dataclass(frozen=True)
class PlannedRecovery:
    action: RecoveryAction
    attempt: int
    not_before: float  # monotonic time when the action may run


class Supervisor:
    def __init__(
        self,
        backoff_base: float = 10.0,
        backoff_max: float = 300.0,
        parked_interval: float = 300.0,
        stable_seconds: float = 600.0,
    ) -> None:
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.parked_interval = parked_interval
        self.stable_seconds = stable_seconds
        self.failures = 0
        self._planned: PlannedRecovery | None = None
        self._connected_since: float | None = None

    @property
    def parked(self) -> bool:
        return self.failures > len(LADDER)

    def on_failure(self, now: float, reason: str = "") -> PlannedRecovery:
        """Record a failure and plan the next recovery action."""
        self.failures += 1
        self._connected_since = None
        if self.failures <= len(LADDER):
            action = LADDER[self.failures - 1]
            wait = min(self.backoff_base * (2 ** (self.failures - 1)), self.backoff_max)
        else:
            action = RecoveryAction.RECONNECT  # parked: gentle retries forever
            wait = self.parked_interval
        self._planned = PlannedRecovery(action, self.failures, now + wait)
        return self._planned

    def due(self, now: float) -> PlannedRecovery | None:
        """The planned action, if its backoff has elapsed."""
        if self._planned and now >= self._planned.not_before:
            return self._planned
        return None

    def consume(self) -> None:
        """Mark the planned action as taken (outcome decides what happens next)."""
        self._planned = None

    def on_connected(self, now: float) -> None:
        """Track stable connectivity; a long-enough stretch resets the ladder."""
        if self._connected_since is None:
            self._connected_since = now
        elif self.failures and now - self._connected_since >= self.stable_seconds:
            self.reset()

    def reset(self) -> None:
        self.failures = 0
        self._planned = None
        self._connected_since = None

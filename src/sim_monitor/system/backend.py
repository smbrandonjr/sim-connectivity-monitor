"""Network backend interface: everything the daemon asks of the host system.

The real implementation (Phase 3) shells out to nmcli/mmcli/ip and sysfs;
FakeBackend simulates it. Methods raise BackendError on failure — the daemon
routes those into the supervisor, never crashes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from sim_monitor.config.schema import Profile


class BackendError(Exception):
    pass


@dataclass(frozen=True)
class ConnectionState:
    active: bool
    interface: str | None = None
    ip_address: str | None = None


class NetworkBackend(ABC):
    @abstractmethod
    def modem_available(self) -> bool:
        """Is a modem visible to the system (mmcli -L)?"""

    @abstractmethod
    def configure_connection(self, profile: Profile) -> None:
        """Create/update the NM GSM connection from the profile's bearer context."""

    @abstractmethod
    def connect(self) -> None:
        """Bring the cellular connection up (nmcli connection up); may raise."""

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def get_connection_state(self) -> ConnectionState: ...

    @abstractmethod
    def verify_routing(self, profile: Profile) -> bool:
        """True if the default-route situation matches the profile's routing prefs."""

    @abstractmethod
    def assert_routing(self, profile: Profile) -> None:
        """Re-apply route metrics if something disturbed them."""

    @abstractmethod
    def modem_disable_enable(self) -> None:
        """Supervisor rung: mmcli --disable / --enable."""

    @abstractmethod
    def usb_power_cycle(self) -> None:
        """Supervisor rung: power-cycle the modem's USB port via sysfs."""

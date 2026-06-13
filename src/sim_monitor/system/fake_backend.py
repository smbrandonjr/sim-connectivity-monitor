"""Simulated network backend for tests and --simulate mode.

The fake "connects" successfully when scripted to (default yes) and tracks the
profile it was configured with, so tests can assert the daemon handed NM the
right bearer APN and metric.
"""

from __future__ import annotations

from sim_monitor.config.schema import Profile
from sim_monitor.modem.fake import FakeModemDriver
from sim_monitor.system.backend import BackendError, ConnectionState, NetworkBackend


class FakeBackend(NetworkBackend):
    def __init__(self, driver: FakeModemDriver | None = None) -> None:
        self.driver = driver  # connect honors airplane/SIM state when provided
        self.configured_profile: Profile | None = None
        self.configured_bearer = None
        self.connected = False
        self.interface = "wwan0"
        self.ip_address = "10.170.42.7"
        self.routing_ok = True
        # Scripting hooks
        self.fail_connect = False
        self.fail_configure = False
        self.drop_connection = False  # next state poll reports the bearer lost
        self.activation_ticks = 0  # >0: report `activating` for N polls first
        self.connect_is_noop = False  # connect() succeeds but nothing happens
        self.disable_enable_calls = 0
        self.usb_cycle_calls = 0
        self._activating = 0

    def modem_available(self) -> bool:
        return self.driver is not None

    def configure_connection(self, profile: Profile, bearer=None) -> None:
        if self.fail_configure:
            raise BackendError("simulated nmcli modify failure")
        self.configured_profile = profile
        self.configured_bearer = bearer or profile.bearer_context

    def connect(self) -> None:
        if self.fail_connect:
            raise BackendError("simulated nmcli up failure")
        if self.configured_profile is None:
            raise BackendError("connection not configured")
        if self.driver is not None:
            if self.driver.airplane:
                raise BackendError("modem is in airplane mode")
            if not self.driver.sim_present:
                raise BackendError("no SIM")
        if self.connect_is_noop:
            return
        if self.activation_ticks > 0:
            self._activating = self.activation_ticks
            return
        self.connected = True
        self.drop_connection = False

    def disconnect(self) -> None:
        self.connected = False
        self._activating = 0

    def get_connection_state(self) -> ConnectionState:
        if self._activating > 0:
            self._activating -= 1
            if self._activating == 0:
                self.connected = True
                self.drop_connection = False
            return ConnectionState(active=False, activating=True)
        if self.drop_connection:
            self.connected = False
            self.drop_connection = False
        if not self.connected:
            return ConnectionState(active=False)
        return ConnectionState(active=True, interface=self.interface, ip_address=self.ip_address)

    def verify_routing(self, profile: Profile) -> bool:
        return self.routing_ok

    def assert_routing(self, profile: Profile) -> None:
        self.routing_ok = True

    def modem_disable_enable(self) -> None:
        self.disable_enable_calls += 1

    def usb_power_cycle(self) -> None:
        self.usb_cycle_calls += 1

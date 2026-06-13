"""Real NetworkBackend: ModemManager/NetworkManager own the bearer, `ip`
verifies routing, sysfs powers the USB port."""

from __future__ import annotations

import logging
from collections.abc import Callable

from sim_monitor.config.schema import Profile
from sim_monitor.system import usb_power
from sim_monitor.system.backend import BackendError, ConnectionState, NetworkBackend
from sim_monitor.system.mmcli import Mmcli
from sim_monitor.system.nmcli import CONNECTION_NAME, Nmcli
from sim_monitor.system.routing import Routing

log = logging.getLogger(__name__)

# Profiles with make_default: false still connect, but lose to ethernet (100)
# and wifi (600) so the cellular link never takes the default route.
NON_DEFAULT_METRIC = 700


def effective_metric(profile: Profile) -> int:
    return profile.routing.metric if profile.routing.make_default else NON_DEFAULT_METRIC


class RealBackend(NetworkBackend):
    def __init__(
        self,
        mmcli: Mmcli,
        nmcli: Nmcli,
        routing: Routing,
        at_port_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.mmcli = mmcli
        self.nmcli = nmcli
        self.routing = routing
        self.at_port_provider = at_port_provider

    def modem_available(self) -> bool:
        try:
            return self.mmcli.first_modem() is not None
        except BackendError as e:
            log.warning("mmcli unavailable: %s", e)
            return False

    def configure_connection(self, profile: Profile, bearer=None) -> None:
        bearer = bearer or profile.bearer_context
        self.nmcli.ensure_gsm_connection(
            apn=bearer.apn,
            username=bearer.username,
            password=bearer.password,
            metric=effective_metric(profile),
        )

    def connect(self) -> None:
        self.nmcli.up(CONNECTION_NAME)  # non-blocking; poll connection_state

    def disconnect(self) -> None:
        self.nmcli.down(CONNECTION_NAME)

    def get_connection_state(self) -> ConnectionState:
        return self.nmcli.connection_state(CONNECTION_NAME)

    def verify_routing(self, profile: Profile) -> bool:
        state = self.get_connection_state()
        if not state.active or not state.interface:
            return False
        if not profile.routing.make_default:
            # Inverted check: the cellular link must NOT be the default.
            return not self.routing.interface_is_default(state.interface)
        return self.routing.interface_is_default(state.interface)

    def assert_routing(self, profile: Profile) -> None:
        self.nmcli.set_route_metric(effective_metric(profile))
        state = self.get_connection_state()
        if state.active and state.interface:
            try:
                self.nmcli.reapply(state.interface)
            except BackendError:
                # reapply can refuse on some device types; cycling the
                # connection is the daemon's job if verify still fails.
                log.debug("nmcli reapply refused; metric applies on next activation")

    def modem_disable_enable(self) -> None:
        index = self.mmcli.first_modem()
        if index is None:
            raise BackendError("no modem visible to ModemManager")
        self.mmcli.disable(index)
        self.mmcli.enable(index)

    def usb_power_cycle(self) -> None:
        port = self.at_port_provider() if self.at_port_provider else None
        if port is None:
            raise BackendError("no AT port known; cannot locate USB device to power-cycle")
        usb_power.power_cycle_tty(port)

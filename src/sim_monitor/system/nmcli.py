"""NetworkManager CLI wrapper: owns the `sim-monitor-cellular` GSM connection.

Terse (-t) output is colon-separated with backslash-escaped colons in values;
_parse_terse handles that. The GSM connection's NM *device* is the control
port (e.g. cdc-wdm0); the routable netdev (e.g. wwan0) is the device's
GENERAL.IP-IFACE.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable

from sim_monitor.system import proc
from sim_monitor.system.backend import BackendError, ConnectionState

log = logging.getLogger(__name__)

CONNECTION_NAME = "sim-monitor-cellular"


def _parse_terse(output: str) -> dict[str, str]:
    """Parse `nmcli -t <...> show` output (FIELD:value per line, \\: escaped)."""
    fields: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        # Split on the first unescaped colon.
        m = re.match(r"((?:[^:\\]|\\.)+):(.*)", line)
        if not m:
            continue
        key = m.group(1).replace("\\:", ":")
        fields[key] = m.group(2).replace("\\:", ":")
    return fields


class Nmcli:
    def __init__(self, runner: Callable[..., str] = proc.run) -> None:
        self._run = runner

    def connection_exists(self, name: str = CONNECTION_NAME) -> bool:
        out = self._run(["nmcli", "-t", "-f", "NAME", "connection", "show"])
        return name in [line.strip() for line in out.splitlines()]

    def ensure_gsm_connection(
        self,
        apn: str,
        username: str = "",
        password: str = "",
        metric: int = 50,
        name: str = CONNECTION_NAME,
    ) -> None:
        if not self.connection_exists(name):
            self._run(
                ["nmcli", "connection", "add", "type", "gsm", "ifname", "*",
                 "con-name", name, "apn", apn]
            )
        self._run(
            ["nmcli", "connection", "modify", name,
             "gsm.apn", apn,
             "gsm.username", username,
             "gsm.password", password,
             "ipv4.route-metric", str(metric),
             "ipv6.route-metric", str(metric),
             # The daemon owns activation. With autoconnect, NM races our
             # explicit `connection up` calls and each new activation request
             # CANCELS the modem's in-flight network registration.
             "connection.autoconnect", "no"]
        )

    def up(self, name: str = CONNECTION_NAME) -> None:
        """Start activation and return immediately (--wait 0).

        Progress is observed via connection_state(); blocking here (and
        re-invoking on timeout) cancels in-flight registration, which can
        take minutes on roaming SIMs."""
        self._run(["nmcli", "--wait", "0", "connection", "up", name], timeout=30)

    def down(self, name: str = CONNECTION_NAME) -> None:
        try:
            self._run(["nmcli", "connection", "down", name])
        except BackendError as e:
            if "not an active connection" in str(e).lower():
                return  # already down
            raise

    def connection_state(self, name: str = CONNECTION_NAME) -> ConnectionState:
        try:
            out = self._run(
                ["nmcli", "-t", "-f",
                 "GENERAL.STATE,GENERAL.DEVICES,IP4.ADDRESS,IP4.GATEWAY",
                 "connection", "show", name]
            )
        except BackendError:
            return ConnectionState(active=False)
        fields = _parse_terse(out)
        general_state = fields.get("GENERAL.STATE", "")
        if general_state.startswith("activating"):
            return ConnectionState(active=False, activating=True)
        if general_state != "activated":
            return ConnectionState(active=False)
        device = fields.get("GENERAL.DEVICES") or None
        ip = fields.get("IP4.ADDRESS[1]")
        ip_address = ip.split("/")[0] if ip else None
        return ConnectionState(
            active=True,
            interface=self.ip_interface(device) if device else None,
            ip_address=ip_address,
            gateway=fields.get("IP4.GATEWAY") or None,
        )

    def ip_interface(self, device: str) -> str:
        """The routable netdev for an NM device (wwan0 for cdc-wdm0)."""
        try:
            out = self._run(
                ["nmcli", "-t", "-f", "GENERAL.IP-IFACE", "device", "show", device]
            )
            iface = _parse_terse(out).get("GENERAL.IP-IFACE", "")
            return iface or device
        except BackendError:
            return device

    def set_route_metric(self, metric: int, name: str = CONNECTION_NAME) -> None:
        self._run(
            ["nmcli", "connection", "modify", name,
             "ipv4.route-metric", str(metric), "ipv6.route-metric", str(metric)]
        )

    def reapply(self, device: str) -> None:
        self._run(["nmcli", "device", "reapply", device])

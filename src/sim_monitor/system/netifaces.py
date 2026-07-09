"""Enumerate the host's usable network interfaces (Linux `ip -j addr`).

Used by the latency monitor to ping each up interface independently, so we can
tell a cellular-only problem from a systemic one. Pure parsing over the `ip`
JSON output keeps it unit-testable; on a host without `ip` (e.g. Windows dev)
it simply returns nothing and callers fall back to simulate behaviour.
"""

from __future__ import annotations

import json

from sim_monitor.system import proc

# Virtual/management netdevs that aren't real egress paths worth measuring.
_SKIP_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "tap", "tun")


def _is_skippable(name: str) -> bool:
    return name == "lo" or name.startswith(_SKIP_PREFIXES)


def parse_up_interfaces(data: list[dict]) -> list[str]:
    """Pure: from parsed `ip -j addr` JSON, return names of interfaces that are
    operationally up and carry a global IPv4 address, minus virtual/loopback
    devices. Order follows the `ip` output (kernel index)."""
    out: list[str] = []
    for link in data:
        name = link.get("ifname")
        if not name or _is_skippable(name):
            continue
        if link.get("operstate") not in ("UP", "UNKNOWN"):
            # UNKNOWN covers point-to-point links (ppp0/wwan with no carrier
            # field) that still have an address; DOWN/LOWERLAYERDOWN are out.
            if "UP" not in (link.get("flags") or []):
                continue
        has_v4 = any(
            a.get("family") == "inet" and a.get("scope") == "global" and a.get("local")
            for a in link.get("addr_info", [])
        )
        if has_v4:
            out.append(name)
    return out


def parse_local_ips(data: list[dict]) -> set[str]:
    """Pure: every address assigned to any interface (v4+v6, loopback too).
    Used to orient conntrack flows as inbound/outbound relative to this host."""
    ips: set[str] = set()
    for link in data:
        for a in link.get("addr_info", []):
            ip = a.get("local")
            if ip:
                ips.add(ip)
    return ips


def list_local_ips(runner=proc.run) -> set[str]:
    """All local addresses, best-effort. Empty if `ip` is absent."""
    try:
        data = json.loads(runner(["ip", "-j", "addr", "show"], timeout=5) or "[]")
    except Exception:  # noqa: BLE001 - no `ip` / non-Linux -> nothing to report
        return set()
    if not isinstance(data, list):
        return set()
    return parse_local_ips(data)


def list_up_interfaces(runner=proc.run) -> list[str]:
    """Up interfaces with a global IPv4, best-effort. Empty if `ip` is absent."""
    try:
        data = json.loads(runner(["ip", "-j", "addr", "show"], timeout=5) or "[]")
    except Exception:  # noqa: BLE001 - no `ip` / non-Linux -> nothing to report
        return []
    if not isinstance(data, list):
        return []
    return parse_up_interfaces(data)

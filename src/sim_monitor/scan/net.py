"""Pure helpers for the network scanner: interface/CIDR enumeration, host and
port expansion, reverse DNS. No long-running I/O here — the engine drives those."""

from __future__ import annotations

import ipaddress
import json
import socket

from sim_monitor.system import proc

# Common service ports probed for host discovery and offered as the default
# port-scan set.
COMMON_PORTS = [21, 22, 23, 25, 53, 80, 110, 139, 143, 443, 445, 3306, 3389, 5432, 8080, 8443]

MAX_HOSTS = 4096   # refuse sweeps bigger than this (~/20)
MAX_PORTS = 4096


def list_interfaces(runner=proc.run) -> list[dict]:
    """Up interfaces with an IPv4 address and their network CIDR, for the picker."""
    try:
        out = runner(["ip", "-j", "addr", "show"], timeout=5)
        data = json.loads(out or "[]")
    except Exception:  # noqa: BLE001 - best-effort; empty list if `ip` unavailable
        return []
    result = []
    for link in data:
        name = link.get("ifname")
        if not name or name == "lo":
            continue
        for addr in link.get("addr_info", []):
            if addr.get("family") != "inet":
                continue
            ip = addr.get("local")
            prefix = addr.get("prefixlen")
            if not ip or prefix is None:
                continue
            try:
                net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
            except ValueError:
                continue
            result.append({"name": name, "ip": ip, "cidr": str(net),
                           "hosts": net.num_addresses})
    return result


def expand_hosts(cidr: str) -> list[str]:
    """Host addresses in a CIDR (or a single IP). Raises ValueError if too large."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as e:
        raise ValueError(f"invalid network {cidr!r}: {e}") from e
    if net.num_addresses == 1:
        return [str(net.network_address)]
    hosts = list(net.hosts())
    if len(hosts) > MAX_HOSTS:
        raise ValueError(f"{cidr} has {len(hosts)} hosts; max {MAX_HOSTS} (use a smaller range)")
    return [str(h) for h in hosts]


def parse_ports(spec: str) -> list[int]:
    """Parse a port spec: 'common', '22,80,443', '1-1024', or a mix."""
    spec = (spec or "").strip().lower()
    if not spec or spec == "common":
        return list(COMMON_PORTS)
    ports: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo_i, hi_i = int(lo), int(hi)
            if not (1 <= lo_i <= hi_i <= 65535):
                raise ValueError(f"invalid port range {part!r}")
            ports.update(range(lo_i, hi_i + 1))
        else:
            p = int(part)
            if not 1 <= p <= 65535:
                raise ValueError(f"invalid port {part!r}")
            ports.add(p)
    if len(ports) > MAX_PORTS:
        raise ValueError(f"{len(ports)} ports requested; max {MAX_PORTS}")
    return sorted(ports)


def reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, socket.herror):
        return None

"""Pure parsing of `conntrack` CLI output into flow records.

The traffic auditor rides on the kernel's connection tracker: every flow the
device originates, receives, or forwards shows up in conntrack with per-
direction packet/byte counters (once nf_conntrack_acct=1). We consume two
output shapes from the conntrack-tools CLI:

Event stream (`conntrack -E -e DESTROY -o timestamp,id`) — one line per flow
as it leaves the table, i.e. the flow's final, authoritative totals:

    [1656942980.634473        ]\t    [DESTROY] tcp      6 src=10.0.0.5 \
    dst=142.250.72.14 sport=48388 dport=443 packets=18 bytes=2214 \
    src=142.250.72.14 dst=10.0.0.5 sport=443 dport=48388 packets=16 \
    bytes=8102 [ASSURED] delta-time=32 id=3735928559

Table dump (`conntrack -L -o id`) — the currently-live flows, used to surface
long-running connections before they close:

    tcp      6 431988 ESTABLISHED src=10.0.0.5 dst=1.2.3.4 sport=51512 \
    dport=443 packets=12 bytes=2345 src=1.2.3.4 dst=10.0.0.5 sport=443 \
    dport=51512 packets=10 bytes=8901 [ASSURED] mark=0 use=1 \
    delta-time=12 id=1443128772

Parsing is tolerant: fields are key=value tokens read in order, the first
`src=` starting the original-direction tuple and the second the reply tuple.
Counters missing (accounting off) read as 0. ICMP tuples carry type/code/id
instead of ports. No I/O here — everything is unit-testable anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TS_RE = re.compile(r"^\[(\d+(?:\.\d+)?)\s*\]\s*")
_EVENT_RE = re.compile(r"^\[(NEW|UPDATE|DESTROY)\]\s*")
_KV_RE = re.compile(r"([\w-]+)=(\S+)")

# key=value keys that belong to the per-direction tuples (everything else,
# e.g. mark/use/delta-time and the trailing global id, is flow-level).
_TUPLE_KEYS = {"src", "dst", "sport", "dport", "type", "code", "id", "packets", "bytes"}


@dataclass
class FlowEvent:
    """One conntrack line: the original-direction tuple plus both directions'
    counters. `src`/`dst` are the flow originator's view (first tuple)."""

    proto: str
    src: str
    dst: str
    sport: int | None
    dport: int | None
    orig_packets: int
    orig_bytes: int
    reply_packets: int
    reply_bytes: int
    event: str | None = None      # NEW/UPDATE/DESTROY; None for -L dumps
    ts: float | None = None       # event timestamp (epoch), from -o timestamp
    state: str | None = None      # TCP state (ESTABLISHED, ...) in -L dumps
    delta_time: int | None = None  # seconds since flow start (needs nf_conntrack_timestamp)
    ct_id: int | None = None      # kernel flow id, from -o id
    icmp_type: int | None = None


def parse_line(line: str) -> FlowEvent | None:
    """Parse one conntrack output line; None for blanks/unrecognised lines."""
    line = line.strip()
    if not line:
        return None

    ts: float | None = None
    m = _TS_RE.match(line)
    if m:
        ts = float(m.group(1))
        line = line[m.end():]
    event: str | None = None
    m = _EVENT_RE.match(line)
    if m:
        event = m.group(1)
        line = line[m.end():]

    # Head tokens before the first key=value: proto name, proto number,
    # [timeout], [TCP state]. Order-tolerant: alphas are name/state, ints skip.
    proto: str | None = None
    state: str | None = None
    for tok in line.split():
        if "=" in tok:
            break
        if tok.startswith("["):  # flag like [UNREPLIED] oddly early — ignore
            continue
        if tok.isdigit():
            continue
        if proto is None:
            proto = tok.lower()
        elif tok.isupper():
            state = tok
    if proto is None:
        return None

    # key=value fields in order; the 1st src= opens the orig tuple, the 2nd
    # opens the reply tuple. ids are collected separately: tcp/udp tuples have
    # none (so 1 id => the global -o id), icmp tuples each carry one (2 ids =>
    # tuple-only, 3 => last is global).
    orig: dict[str, str] = {}
    reply: dict[str, str] = {}
    flow: dict[str, str] = {}
    ids: list[str] = []
    side: dict[str, str] | None = None
    for key, val in _KV_RE.findall(line):
        if key == "id":
            ids.append(val)
            continue
        if key == "src":
            side = orig if side is None else reply
        if key in _TUPLE_KEYS and side is not None:
            side[key] = val
        else:
            flow[key] = val
    if "src" not in orig or "dst" not in orig:
        return None

    ct_id = int(ids[-1]) if len(ids) % 2 == 1 else None

    def _int(d: dict[str, str], key: str) -> int | None:
        try:
            return int(d[key])
        except (KeyError, ValueError):
            return None

    return FlowEvent(
        proto=proto,
        src=orig["src"],
        dst=orig["dst"],
        sport=_int(orig, "sport"),
        dport=_int(orig, "dport"),
        orig_packets=_int(orig, "packets") or 0,
        orig_bytes=_int(orig, "bytes") or 0,
        reply_packets=_int(reply, "packets") or 0,
        reply_bytes=_int(reply, "bytes") or 0,
        event=event,
        ts=ts,
        state=state,
        delta_time=_int(flow, "delta-time"),
        ct_id=ct_id,
        icmp_type=_int(orig, "type"),
    )


def parse_lines(text: str) -> list[FlowEvent]:
    """Parse a multi-line dump (`conntrack -L` stdout), skipping junk lines."""
    out = []
    for line in text.splitlines():
        ev = parse_line(line)
        if ev is not None:
            out.append(ev)
    return out


# ── direction classification ─────────────────────────────────────────────────


@dataclass
class ClassifiedFlow:
    """A FlowEvent normalised to the device's point of view, so 'traffic to
    IP X on port Y' is a direct query regardless of who dialled."""

    direction: str          # 'out' | 'in' | 'fwd' | 'local'
    remote_ip: str
    remote_port: int | None
    local_ip: str
    local_port: int | None
    bytes_sent: int         # bytes the device (or LAN client, for fwd) sent
    bytes_recv: int
    packets_sent: int
    packets_recv: int


def _is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def _is_multicast_or_broadcast(ip: str) -> bool:
    if ip == "255.255.255.255":
        return True
    first = ip.split(".", 1)[0]
    if first.isdigit() and 224 <= int(first) <= 239:  # IPv4 multicast
        return True
    return ip.lower().startswith("ff")  # IPv6 multicast


def classify(ev: FlowEvent, local_ips: set[str]) -> ClassifiedFlow:
    """Orient a flow relative to this device using its local IP set: the side
    holding a local address is 'local', the other 'remote'. Flows where
    neither end is local are traffic we forwarded/routed ('fwd', oriented by
    originator); both ends local (incl. loopback) is 'local' chatter."""
    src_local = ev.src in local_ips or _is_loopback(ev.src)
    # Multicast/broadcast to us counts as delivered here even though the
    # group address isn't in our local IP set.
    dst_local = (
        ev.dst in local_ips
        or _is_loopback(ev.dst)
        or _is_multicast_or_broadcast(ev.dst)
    )
    if dst_local and not src_local:
        # Dialled from outside: remote originated, orig counters are received.
        return ClassifiedFlow(
            direction="in",
            remote_ip=ev.src, remote_port=ev.sport,
            local_ip=ev.dst, local_port=ev.dport,
            bytes_sent=ev.reply_bytes, bytes_recv=ev.orig_bytes,
            packets_sent=ev.reply_packets, packets_recv=ev.orig_packets,
        )
    if src_local and dst_local:
        direction = "local"
    elif src_local:
        direction = "out"
    else:
        direction = "fwd"
    return ClassifiedFlow(
        direction=direction,
        remote_ip=ev.dst, remote_port=ev.dport,
        local_ip=ev.src, local_port=ev.sport,
        bytes_sent=ev.orig_bytes, bytes_recv=ev.reply_bytes,
        packets_sent=ev.orig_packets, packets_recv=ev.reply_packets,
    )

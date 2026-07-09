"""Flow-event sources for the traffic auditor.

ConntrackSource is the real thing: it enables kernel flow accounting, streams
flow-close (DESTROY) events from `conntrack -E`, and dumps the live table with
`conntrack -L` for checkpoints. FakeFlowSource emits plausible synthetic flows
so the whole feature runs in --simulate mode on a dev box.

Both expose the same small surface the collector polls:
    setup() -> list[str]      one-time enablement; returns warnings
    start() / stop()          own the event stream
    running -> bool           event stream alive?
    drain() -> list[FlowEvent]   events since the last drain (non-blocking)
    snapshot() -> list[FlowEvent] | None   current live flows (None = failed)
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
import time
from pathlib import Path

from sim_monitor.system import proc
from sim_monitor.system.backend import BackendError
from sim_monitor.traffic.parse import FlowEvent, parse_line, parse_lines

log = logging.getLogger(__name__)

_EVENT_CMD = ["conntrack", "-E", "-e", "DESTROY", "-o", "timestamp,id"]
_DUMP_CMD = ["conntrack", "-L", "-o", "id"]
_SYSCTL_DIR = Path("/proc/sys/net/netfilter")
# nftables table whose sole job is holding a conntrack expression, which makes
# the kernel register its tracking hooks even when no firewall is configured.
# The rule has no verdict, so it never affects traffic.
_NFT_TABLE = "sim_monitor_audit"


class ConntrackSource:
    def __init__(self, runner=proc.run, popen=subprocess.Popen) -> None:
        self._run = runner
        self._popen = popen
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue[FlowEvent] = queue.Queue()
        self._reader: threading.Thread | None = None

    def setup(self) -> list[str]:
        """Enable per-flow byte accounting + start timestamps and make sure
        conntrack is actually tracking. Idempotent; each step degrades to a
        warning so partial capability still audits what it can."""
        warnings: list[str] = []
        try:  # usually built-in or auto-loaded; best-effort
            self._run(["modprobe", "nf_conntrack"], timeout=10)
        except BackendError:
            pass
        for name in ("nf_conntrack_acct", "nf_conntrack_timestamp"):
            try:
                (_SYSCTL_DIR / name).write_text("1")
            except OSError as e:
                warnings.append(f"couldn't enable {name}: {e}")
        try:
            self._run(["nft", "list", "table", "inet", _NFT_TABLE], timeout=10)
        except BackendError:
            try:
                self._run(["nft", "add", "table", "inet", _NFT_TABLE], timeout=10)
                self._run(
                    ["nft", "add", "chain", "inet", _NFT_TABLE, "audit",
                     "{ type filter hook prerouting priority -150 ; policy accept ; }"],
                    timeout=10,
                )
                self._run(
                    ["nft", "add", "rule", "inet", _NFT_TABLE, "audit",
                     "ct", "state", "new,established,related", "counter"],
                    timeout=10,
                )
            except BackendError as e:
                warnings.append(f"couldn't install conntrack-enable nft table: {e}")
        return warnings

    def start(self) -> None:
        """Spawn the event stream; raises BackendError if conntrack is absent."""
        self.stop()
        try:
            self._proc = self._popen(
                _EVENT_CMD, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1,
            )
        except OSError as e:
            self._proc = None
            raise BackendError(f"conntrack not available: {e}") from e
        self._reader = threading.Thread(
            target=self._read_events, args=(self._proc,),
            name="conntrack-events", daemon=True,
        )
        self._reader.start()

    def _read_events(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout or ():
                ev = parse_line(line)
                if ev is not None:
                    self._queue.put(ev)
        except Exception:  # noqa: BLE001 - reader must never take the app down
            log.exception("conntrack event reader failed")

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def drain(self) -> list[FlowEvent]:
        out: list[FlowEvent] = []
        while True:
            try:
                out.append(self._queue.get_nowait())
            except queue.Empty:
                return out

    def snapshot(self) -> list[FlowEvent] | None:
        try:
            return parse_lines(self._run(_DUMP_CMD, timeout=15))
        except BackendError as e:
            log.warning("conntrack table dump failed: %s", e)
            return None

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - already-dead process is fine
                pass
            self._proc = None


# ── simulate mode ────────────────────────────────────────────────────────────

# The fake device's own addresses (cellular + LAN + loopback).
SIMULATE_LOCAL_IPS = {"10.170.42.7", "192.168.1.50", "127.0.0.1"}

# (proto, remote_ip, remote_port, ~out bytes, ~in bytes, mean seconds between)
_FAKE_TEMPLATES = [
    ("udp", "1.1.1.1", 53, 74, 158, 12.0),           # DNS
    ("udp", "129.6.15.28", 123, 76, 76, 45.0),       # NTP
    ("tcp", "34.110.222.30", 443, 2900, 1400, 30.0),  # heartbeat POST
    ("tcp", "18.205.93.2", 443, 5200, 88000, 90.0),  # occasional API pull
]


class FakeFlowSource:
    """Synthesises outbound app-ish flows plus inbound LAN hits on the web UI,
    and one long-lived active connection whose counters keep growing."""

    def __init__(self, rng=None, clock=time.time) -> None:
        import random

        self._rand = rng or random.Random()
        self._clock = clock
        self._started_at: float | None = None
        self._last_drain: float | None = None
        self._next_ct_id = 1000
        self.running = False

    def local_ips(self) -> set[str]:
        return set(SIMULATE_LOCAL_IPS)

    def setup(self) -> list[str]:
        return []

    def start(self) -> None:
        self.running = True
        now = self._clock()
        self._started_at = now
        self._last_drain = now

    def stop(self) -> None:
        self.running = False

    def _flow(self, proto, src, dst, sport, dport, out_b, in_b,
              ct_id, event, delta) -> FlowEvent:
        jitter = 0.5 + self._rand.random()
        out_bytes = int(out_b * jitter)
        in_bytes = int(in_b * jitter)
        return FlowEvent(
            proto=proto, src=src, dst=dst, sport=sport, dport=dport,
            orig_packets=max(1, out_bytes // 900), orig_bytes=out_bytes,
            reply_packets=max(1, in_bytes // 900), reply_bytes=in_bytes,
            event=event, ts=self._clock(), delta_time=delta, ct_id=ct_id,
        )

    def drain(self) -> list[FlowEvent]:
        if not self.running or self._last_drain is None:
            return []
        now = self._clock()
        elapsed = now - self._last_drain
        self._last_drain = now
        out: list[FlowEvent] = []
        for proto, ip, port, out_b, in_b, mean_gap in _FAKE_TEMPLATES:
            if self._rand.random() < min(1.0, elapsed / mean_gap):
                self._next_ct_id += 1
                out.append(self._flow(
                    proto, "10.170.42.7", ip, self._rand.randint(32768, 60999),
                    port, out_b, in_b, self._next_ct_id, "DESTROY",
                    self._rand.randint(1, 30),
                ))
        # Inbound: someone on the LAN polling the web UI.
        if self._rand.random() < min(1.0, elapsed / 25.0):
            self._next_ct_id += 1
            out.append(self._flow(
                "tcp", "192.168.1.23", "192.168.1.50",
                self._rand.randint(32768, 60999), 8080,
                900, 24000, self._next_ct_id, "DESTROY",
                self._rand.randint(1, 5),
            ))
        return out

    def snapshot(self) -> list[FlowEvent] | None:
        if not self.running or self._started_at is None:
            return []
        # One long-lived TLS session (e.g. a keepalive tunnel) that stays in
        # the live table, counters marching upward.
        age = int(self._clock() - self._started_at)
        return [FlowEvent(
            proto="tcp", src="10.170.42.7", dst="52.44.11.90",
            sport=40222, dport=443,
            orig_packets=10 + age, orig_bytes=1200 + 90 * age,
            reply_packets=8 + age, reply_bytes=1000 + 70 * age,
            state="ESTABLISHED", delta_time=age, ct_id=999001,
        )]

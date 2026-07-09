"""Traffic auditor: conntrack parsing, flow classification, DB queries, and
the collector's row lifecycle (destroy-finalise, live checkpoints, stale
close, retention)."""

import pytest

from sim_monitor.config.schema import TrafficConfig
from sim_monitor.core.events import EventLog
from sim_monitor.storage.db import Database
from sim_monitor.system.backend import BackendError
from sim_monitor.traffic.collector import TrafficCollector, effective_traffic_config
from sim_monitor.traffic.parse import FlowEvent, classify, parse_line, parse_lines
from sim_monitor.traffic.sources import ConntrackSource, FakeFlowSource

LOCAL = {"10.0.0.5", "192.168.1.50"}

DESTROY_TCP = (
    "[1656942980.634473        ]\t    [DESTROY] tcp      6 "
    "src=10.0.0.5 dst=142.250.72.14 sport=48388 dport=443 packets=18 bytes=2214 "
    "src=142.250.72.14 dst=10.0.0.5 sport=443 dport=48388 packets=16 bytes=8102 "
    "[ASSURED] delta-time=32 id=3735928559"
)
DESTROY_UDP = (
    "[1656942981.1 ]\t    [DESTROY] udp      17 "
    "src=10.0.0.5 dst=1.1.1.1 sport=41234 dport=53 packets=1 bytes=74 "
    "src=1.1.1.1 dst=10.0.0.5 sport=53 dport=41234 packets=1 bytes=158 id=123"
)
DESTROY_ICMP = (
    "[1656942982.2 ]\t    [DESTROY] icmp     1 "
    "src=10.0.0.5 dst=8.8.8.8 type=8 code=0 id=51477 packets=5 bytes=420 "
    "src=8.8.8.8 dst=10.0.0.5 type=0 code=0 id=51477 packets=5 bytes=420 id=999"
)
LIST_TCP = (
    "tcp      6 431988 ESTABLISHED "
    "src=192.168.1.50 dst=52.44.11.90 sport=40222 dport=443 packets=12 bytes=2345 "
    "src=52.44.11.90 dst=192.168.1.50 sport=443 dport=40222 packets=10 bytes=8901 "
    "[ASSURED] mark=0 use=1 delta-time=12 id=1443128772"
)
LIST_UDP_UNREPLIED = (
    "udp      17 25 "
    "src=10.0.0.5 dst=9.9.9.9 sport=1000 dport=53 packets=1 bytes=76 [UNREPLIED] "
    "src=9.9.9.9 dst=10.0.0.5 sport=53 dport=1000 packets=0 bytes=0 mark=0 use=1 id=77"
)
LIST_TCP_V6 = (
    "tcp      6 300 ESTABLISHED "
    "src=2001:db8::1 dst=2607:f8b0::200e sport=5555 dport=443 packets=3 bytes=333 "
    "src=2607:f8b0::200e dst=2001:db8::1 sport=443 dport=5555 packets=2 bytes=222 "
    "[ASSURED] mark=0 use=1 id=42"
)
# Accounting off: no packets/bytes fields at all.
LIST_NO_ACCT = (
    "tcp      6 100 ESTABLISHED src=10.0.0.5 dst=4.4.4.4 sport=1 dport=2 "
    "src=4.4.4.4 dst=10.0.0.5 sport=2 dport=1 [ASSURED] mark=0 use=1 id=8"
)


class TestParseLine:
    def test_destroy_tcp(self):
        ev = parse_line(DESTROY_TCP)
        assert ev.event == "DESTROY"
        assert ev.ts == pytest.approx(1656942980.634473)
        assert ev.proto == "tcp"
        assert (ev.src, ev.dst) == ("10.0.0.5", "142.250.72.14")
        assert (ev.sport, ev.dport) == (48388, 443)
        assert (ev.orig_packets, ev.orig_bytes) == (18, 2214)
        assert (ev.reply_packets, ev.reply_bytes) == (16, 8102)
        assert ev.delta_time == 32
        assert ev.ct_id == 3735928559

    def test_destroy_udp(self):
        ev = parse_line(DESTROY_UDP)
        assert ev.proto == "udp"
        assert ev.dport == 53
        assert ev.reply_bytes == 158
        assert ev.ct_id == 123

    def test_destroy_icmp_tuple_ids_dont_shadow_global(self):
        ev = parse_line(DESTROY_ICMP)
        assert ev.proto == "icmp"
        assert ev.sport is None and ev.dport is None
        assert ev.icmp_type == 8
        assert ev.ct_id == 999  # the trailing -o id, not the tuple echo ids

    def test_list_tcp(self):
        ev = parse_line(LIST_TCP)
        assert ev.event is None and ev.ts is None
        assert ev.state == "ESTABLISHED"
        assert ev.delta_time == 12
        assert ev.ct_id == 1443128772
        assert ev.orig_bytes == 2345 and ev.reply_bytes == 8901

    def test_list_udp_unreplied(self):
        ev = parse_line(LIST_UDP_UNREPLIED)
        assert ev.proto == "udp"
        assert ev.reply_bytes == 0
        assert ev.ct_id == 77

    def test_list_ipv6(self):
        ev = parse_line(LIST_TCP_V6)
        assert ev.src == "2001:db8::1"
        assert ev.dst == "2607:f8b0::200e"
        assert ev.ct_id == 42

    def test_accounting_off_counts_zero(self):
        ev = parse_line(LIST_NO_ACCT)
        assert ev.orig_bytes == 0 and ev.reply_bytes == 0

    def test_junk_lines_skipped(self):
        assert parse_line("") is None
        assert parse_line("conntrack v1.4.7: 42 flow entries have been shown.") is None
        assert parse_lines(f"{DESTROY_TCP}\n\nnot a flow\n{DESTROY_UDP}\n") is not None
        assert len(parse_lines(f"{DESTROY_TCP}\njunk\n{DESTROY_UDP}\n")) == 2


class TestClassify:
    def test_outbound(self):
        c = classify(parse_line(DESTROY_TCP), LOCAL)
        assert c.direction == "out"
        assert (c.remote_ip, c.remote_port) == ("142.250.72.14", 443)
        assert (c.local_ip, c.local_port) == ("10.0.0.5", 48388)
        assert (c.bytes_sent, c.bytes_recv) == (2214, 8102)

    def test_inbound_counters_swap(self):
        ev = FlowEvent(
            proto="tcp", src="203.0.113.9", dst="192.168.1.50", sport=55555,
            dport=8080, orig_packets=10, orig_bytes=900,
            reply_packets=25, reply_bytes=22000,
        )
        c = classify(ev, LOCAL)
        assert c.direction == "in"
        assert (c.remote_ip, c.remote_port) == ("203.0.113.9", 55555)
        assert (c.local_ip, c.local_port) == ("192.168.1.50", 8080)
        assert c.bytes_sent == 22000 and c.bytes_recv == 900

    def test_forwarded(self):
        ev = FlowEvent(
            proto="tcp", src="192.168.1.99", dst="1.2.3.4", sport=1, dport=443,
            orig_packets=1, orig_bytes=10, reply_packets=1, reply_bytes=20,
        )
        assert classify(ev, LOCAL).direction == "fwd"

    def test_local_and_loopback(self):
        ev = FlowEvent(
            proto="tcp", src="127.0.0.1", dst="127.0.0.1", sport=1, dport=8080,
            orig_packets=1, orig_bytes=1, reply_packets=1, reply_bytes=1,
        )
        assert classify(ev, LOCAL).direction == "local"

    def test_inbound_multicast(self):
        ev = FlowEvent(
            proto="udp", src="192.168.1.99", dst="239.255.255.250", sport=1,
            dport=1900, orig_packets=1, orig_bytes=100,
            reply_packets=0, reply_bytes=0,
        )
        c = classify(ev, LOCAL)
        assert c.direction == "in"
        assert c.remote_ip == "192.168.1.99"


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def _row(**kw):
    base = {
        "first_seen": 1000.0, "last_seen": 1010.0, "proto": "tcp",
        "direction": "out", "remote_ip": "1.2.3.4", "remote_port": 443,
        "local_ip": "10.0.0.5", "local_port": 40000,
        "bytes_sent": 100, "bytes_recv": 200,
        "packets_sent": 2, "packets_recv": 3, "active": 0,
    }
    base.update(kw)
    return base


class TestTrafficDb:
    def test_insert_query_filters(self, db):
        db.add_traffic_flow(_row())
        db.add_traffic_flow(_row(remote_ip="5.6.7.8", remote_port=53, proto="udp"))
        db.add_traffic_flow(_row(direction="in", remote_ip="192.168.1.9",
                                 local_port=8080, remote_port=51000))
        rows, total = db.query_traffic_flows()
        assert total == 3
        _, n = db.query_traffic_flows(ip="1.2.3.4")
        assert n == 1
        _, n = db.query_traffic_flows(ip="192.168.1.*")
        assert n == 1
        _, n = db.query_traffic_flows(port=8080)  # matches local or remote side
        assert n == 1
        _, n = db.query_traffic_flows(port=443)
        assert n == 1
        _, n = db.query_traffic_flows(proto="udp")
        assert n == 1
        _, n = db.query_traffic_flows(direction="in")
        assert n == 1

    def test_interface_filter_and_breakdown(self, db):
        db.add_traffic_flow(_row(interface="wwan0", bytes_sent=1000))
        db.add_traffic_flow(_row(interface="wwan0", bytes_sent=500))
        db.add_traffic_flow(_row(interface="wlan0", bytes_sent=10,
                                 direction="in", remote_ip="192.168.1.9"))
        db.add_traffic_flow(_row(interface=None, direction="fwd"))
        _, n = db.query_traffic_flows(interface="wwan0")
        assert n == 2
        by_iface = {r["interface"]: r for r in db.traffic_summary()["by_interface"]}
        assert by_iface["wwan0"]["bytes_sent"] == 1500
        assert by_iface["wwan0"]["flows"] == 2
        assert by_iface["wlan0"]["flows"] == 1
        assert None in by_iface  # unattributed (forwarded) still counted

    def test_window_overlap(self, db):
        db.add_traffic_flow(_row(first_seen=100, last_seen=200))
        # Long-lived flow spanning the whole window still matches.
        db.add_traffic_flow(_row(first_seen=50, last_seen=5000))
        _, n = db.query_traffic_flows(t0=150, t1=160)
        assert n == 2
        _, n = db.query_traffic_flows(t0=300, t1=400)
        assert n == 1
        _, n = db.query_traffic_flows(t0=6000)
        assert n == 0

    def test_update_and_close(self, db):
        rid = db.add_traffic_flow(_row(active=1))
        db.update_traffic_flow(rid, 2000.0, 500, 600, 5, 6, active=True)
        rows, _ = db.query_traffic_flows(active=True)
        assert rows[0]["bytes_sent"] == 500 and rows[0]["last_seen"] == 2000.0
        db.close_traffic_flows([rid])
        _, n = db.query_traffic_flows(active=True)
        assert n == 0

    def test_summary(self, db):
        db.add_traffic_flow(_row(bytes_sent=100, bytes_recv=1000))
        db.add_traffic_flow(_row(remote_ip="9.9.9.9", bytes_sent=50, bytes_recv=10))
        db.add_traffic_flow(_row(direction="in", remote_ip="192.168.1.9",
                                 local_port=8080, bytes_sent=7000, bytes_recv=300,
                                 active=1))
        s = db.traffic_summary()
        assert s["totals"]["out"]["flows"] == 2
        assert s["totals"]["out"]["bytes_sent"] == 150
        assert s["totals"]["in"]["bytes_sent"] == 7000
        assert s["active_flows"] == 1
        assert s["distinct_remotes"] == 3
        assert s["top_remotes"][0]["remote_ip"] == "192.168.1.9"
        ports = {(p["port"], p["proto"]) for p in s["top_ports"]}
        assert (8080, "tcp") in ports  # inbound flows report OUR port
        assert (443, "tcp") in ports

    def test_prune_retention_and_cap(self, db):
        for i in range(10):
            db.add_traffic_flow(_row(first_seen=i, last_seen=i))
        live = db.add_traffic_flow(_row(first_seen=0, last_seen=0, active=1))
        db.prune_traffic_flows(cutoff=5.0, max_rows=3)
        rows, total = db.query_traffic_flows()
        # Old closed rows go; the live checkpoint survives both passes.
        assert live in [r["id"] for r in rows]
        assert total <= 4


class ScriptedSource:
    """Deterministic source: pre-loaded event batches + snapshots."""

    def __init__(self):
        self.batches: list[list[FlowEvent]] = []
        self.snaps: list[list[FlowEvent] | None] = []
        self.running = False
        self.stops = 0

    def setup(self):
        return []

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        self.stops += 1

    def drain(self):
        return self.batches.pop(0) if self.batches else []

    def snapshot(self):
        return self.snaps.pop(0) if self.snaps else []


def _destroy(ct_id=1, **kw):
    base = dict(
        proto="tcp", src="10.0.0.5", dst="1.2.3.4", sport=40000, dport=443,
        orig_packets=2, orig_bytes=100, reply_packets=3, reply_bytes=200,
        event="DESTROY", ts=1000.0, delta_time=10, ct_id=ct_id,
    )
    base.update(kw)
    return FlowEvent(**base)


def _live(ct_id=500, orig_bytes=1000, **kw):
    base = dict(
        proto="tcp", src="10.0.0.5", dst="5.6.7.8", sport=41000, dport=443,
        orig_packets=5, orig_bytes=orig_bytes, reply_packets=4, reply_bytes=800,
        state="ESTABLISHED", delta_time=60, ct_id=ct_id,
    )
    base.update(kw)
    return FlowEvent(**base)


@pytest.fixture
def collector(db):
    src = ScriptedSource()
    clock = {"wall": 1000.0, "mono": 0.0}
    config = TrafficConfig(snapshot_interval_seconds=30)
    col = TrafficCollector(
        db=db,
        events=EventLog(db),
        get_config=lambda: config,
        source=src,
        ip_interfaces=lambda: {"10.0.0.5": "wwan0"},
        backend_name="test",
        wall_clock=lambda: clock["wall"],
        monotonic=lambda: clock["mono"],
    )
    return col, src, clock, config


class TestCollector:
    def test_destroy_becomes_final_row(self, collector):
        col, src, clock, _ = collector
        src.batches = [[_destroy()]]
        col.tick()
        rows, total = col.db.query_traffic_flows()
        assert total == 1
        r = rows[0]
        assert r["active"] == 0
        assert r["direction"] == "out"
        assert r["interface"] == "wwan0"  # attributed from local_ip at capture
        assert r["first_seen"] == pytest.approx(990.0)  # ts - delta_time
        assert r["last_seen"] == pytest.approx(1000.0)

    def test_live_checkpoint_update_then_finalise(self, collector):
        col, src, clock, _ = collector
        src.snaps = [[_live(orig_bytes=1000)]]
        col.tick()
        rows, _ = col.db.query_traffic_flows(active=True)
        assert len(rows) == 1 and rows[0]["bytes_sent"] == 1000
        rid = rows[0]["id"]
        # Next snapshot: same flow, more bytes -> same row updated.
        clock["mono"] += 31
        src.snaps = [[_live(orig_bytes=2000)]]
        col.tick()
        rows, total = col.db.query_traffic_flows()
        assert total == 1 and rows[0]["id"] == rid
        assert rows[0]["bytes_sent"] == 2000
        # DESTROY finalises the same row with authoritative totals.
        src.batches = [[_destroy(ct_id=500, dst="5.6.7.8", sport=41000,
                                 orig_bytes=2500, reply_bytes=900)]]
        col.tick()
        rows, total = col.db.query_traffic_flows()
        assert total == 1 and rows[0]["id"] == rid
        assert rows[0]["active"] == 0 and rows[0]["bytes_sent"] == 2500

    def test_stale_checkpoint_closed_when_flow_vanishes(self, collector):
        col, src, clock, _ = collector
        src.snaps = [[_live()]]
        col.tick()
        clock["mono"] += 31
        src.snaps = [[]]  # flow gone, DESTROY was missed
        col.tick()
        _, active = col.db.query_traffic_flows(active=True)
        assert active == 0
        _, total = col.db.query_traffic_flows()
        assert total == 1

    def test_local_flows_skipped_by_default(self, collector):
        col, src, clock, config = collector
        src.batches = [[_destroy(src="127.0.0.1", dst="127.0.0.1")]]
        col.tick()
        _, total = col.db.query_traffic_flows()
        assert total == 0

    def test_disable_stops_source_and_closes_live(self, collector):
        col, src, clock, config = collector
        src.snaps = [[_live()]]
        col.tick()
        assert src.running
        config.enabled = False
        col.tick()
        assert not src.running and src.stops == 1
        _, active = col.db.query_traffic_flows(active=True)
        assert active == 0

    def test_restart_closes_orphaned_active_rows(self, db):
        # Rows left active by a crashed previous run get closed on startup.
        db.add_traffic_flow(_row(active=1))
        src = ScriptedSource()
        col = TrafficCollector(
            db=db, events=EventLog(db), get_config=TrafficConfig,
            source=src, ip_interfaces=lambda: {"10.0.0.5": "wwan0"},
            backend_name="test",
        )
        col.tick()
        _, active = db.query_traffic_flows(active=True)
        assert active == 0


class TestEffectiveConfig:
    def test_db_setting_overrides_default(self, db):
        default = TrafficConfig(retention_days=30)
        assert effective_traffic_config(db, default).retention_days == 30
        db.set_setting("traffic", {"retention_days": 7})
        assert effective_traffic_config(db, default).retention_days == 7

    def test_invalid_setting_falls_back(self, db):
        db.set_setting("traffic", {"retention_days": "nonsense"})
        assert effective_traffic_config(db, TrafficConfig()).retention_days == 30


class TestSources:
    def test_fake_source_produces_flows(self):
        now = [1000.0]
        src = FakeFlowSource(clock=lambda: now[0])
        src.start()
        events = []
        for _ in range(60):
            now[0] += 10.0
            events.extend(src.drain())
        assert events, "fake source produced nothing over 10 simulated minutes"
        assert all(e.event == "DESTROY" for e in events)
        snap = src.snapshot()
        assert snap and snap[0].ct_id is not None
        first = snap[0].orig_bytes
        now[0] += 60
        assert src.snapshot()[0].orig_bytes > first  # counters march upward

    def test_conntrack_start_missing_binary(self):
        def no_popen(*a, **k):
            raise FileNotFoundError("conntrack")

        src = ConntrackSource(popen=no_popen)
        with pytest.raises(BackendError):
            src.start()
        assert not src.running

    def test_conntrack_setup_installs_nft_table(self):
        calls = []

        def runner(args, timeout=None):
            calls.append(args)
            if args[:2] == ["nft", "list"]:
                raise BackendError("no such table")
            return ""

        src = ConntrackSource(runner=runner)
        src.setup()  # sysctl writes fail off-Linux -> warnings, not errors
        nft_cmds = [c for c in calls if c[0] == "nft"]
        assert ["nft", "add", "table", "inet", "sim_monitor_audit"] in nft_cmds
        assert any(c[1] == "add" and c[2] == "chain" for c in nft_cmds)
        assert any(c[1] == "add" and c[2] == "rule" for c in nft_cmds)

    def test_conntrack_snapshot_parses_dump(self):
        src = ConntrackSource(runner=lambda args, timeout=None: f"{LIST_TCP}\n")
        snap = src.snapshot()
        assert len(snap) == 1 and snap[0].ct_id == 1443128772

    def test_conntrack_snapshot_failure_returns_none(self):
        def runner(args, timeout=None):
            raise BackendError("boom")

        assert ConntrackSource(runner=runner).snapshot() is None

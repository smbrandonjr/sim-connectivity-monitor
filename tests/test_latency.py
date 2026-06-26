"""Tests for the per-interface latency / packet-loss monitor: ping parsing,
interface enumeration, pure rollup aggregation, the PingMonitor iteration, and
the DB layer round-trip."""

from __future__ import annotations

import threading

from sim_monitor.core import latency as agg
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor.ping_monitor import PingMonitor, make_fake_pinger
from sim_monitor.scan.engine import ping_host
from sim_monitor.storage.db import Database
from sim_monitor.system import netifaces

# ── ping_host parsing ────────────────────────────────────────────────────────

_PING_OK = """\
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.

--- 1.1.1.1 ping statistics ---
5 packets transmitted, 5 received, 0% packet loss, time 4006ms
rtt min/avg/max/mdev = 10.123/12.456/15.789/1.234 ms
"""

_PING_PARTIAL = """\
--- 8.8.8.8 ping statistics ---
5 packets transmitted, 3 received, 40% packet loss, time 4010ms
rtt min/avg/max/mdev = 20.0/25.5/30.0/3.0 ms
"""

_PING_TOTAL_LOSS = """\
--- 9.9.9.9 ping statistics ---
5 packets transmitted, 0 received, 100% packet loss, time 4015ms
"""


class TestPingParse:
    def test_full_success(self):
        r = ping_host("1.1.1.1", runner=lambda *a, **k: _PING_OK)
        assert (r["sent"], r["received"], r["loss_pct"]) == (5, 5, 0.0)
        assert r["min_ms"] == 10.123
        assert r["avg_ms"] == 12.456
        assert r["max_ms"] == 15.789

    def test_partial_loss(self):
        r = ping_host("8.8.8.8", runner=lambda *a, **k: _PING_PARTIAL)
        assert r["received"] == 3
        assert r["loss_pct"] == 40.0
        assert r["min_ms"] == 20.0 and r["max_ms"] == 30.0

    def test_total_loss_has_null_rtt(self):
        r = ping_host("9.9.9.9", runner=lambda *a, **k: _PING_TOTAL_LOSS)
        assert r["received"] == 0
        assert r["loss_pct"] == 100.0
        assert r["avg_ms"] is None and r["min_ms"] is None and r["max_ms"] is None

    def test_runner_exception_is_total_loss(self):
        def boom(*a, **k):
            raise OSError("no ping binary")
        r = ping_host("1.1.1.1", runner=boom)
        assert r["loss_pct"] == 100.0 and r["sent"] == 0

    def test_interface_flag_passed(self):
        seen = {}

        def runner(args, timeout=None):
            seen["args"] = args
            return _PING_OK
        ping_host("1.1.1.1", interface="wwan0", count=5, runner=runner)
        assert "-I" in seen["args"] and "wwan0" in seen["args"]
        assert "1.1.1.1" == seen["args"][-1]


# ── interface enumeration ────────────────────────────────────────────────────

_IP_JSON = [
    {"ifname": "lo", "operstate": "UNKNOWN",
     "addr_info": [{"family": "inet", "scope": "host", "local": "127.0.0.1"}]},
    {"ifname": "eth0", "operstate": "UP",
     "addr_info": [{"family": "inet", "scope": "global", "local": "192.168.1.5"}]},
    {"ifname": "wlan0", "operstate": "UP",
     "addr_info": [{"family": "inet6", "scope": "global", "local": "fe80::1"},
                   {"family": "inet", "scope": "global", "local": "192.168.1.6"}]},
    {"ifname": "wwan0", "operstate": "UNKNOWN", "flags": ["UP"],
     "addr_info": [{"family": "inet", "scope": "global", "local": "10.170.42.7"}]},
    {"ifname": "eth1", "operstate": "DOWN",
     "addr_info": [{"family": "inet", "scope": "global", "local": "10.0.0.9"}]},
    {"ifname": "docker0", "operstate": "UP",
     "addr_info": [{"family": "inet", "scope": "global", "local": "172.17.0.1"}]},
]


class TestNetifaces:
    def test_filters_to_up_global_ipv4_real_devs(self):
        names = netifaces.parse_up_interfaces(_IP_JSON)
        assert names == ["eth0", "wlan0", "wwan0"]  # lo/eth1(down)/docker0 dropped

    def test_empty_when_ip_missing(self):
        def boom(*a, **k):
            raise OSError("no ip")
        assert netifaces.list_up_interfaces(runner=boom) == []


# ── pure aggregation (bucketing + summarize) ─────────────────────────────────

class TestBucketing:
    def test_groups_by_hour_interface_target(self):
        base = 1_000_000.0  # arbitrary epoch; floor to hour boundary later
        h0 = agg.bucket_start(base, "hour")
        samples = [
            {"ts": h0 + 10, "interface": "wwan0", "target": "1.1.1.1",
             "sent": 5, "received": 5, "loss_pct": 0.0,
             "rtt_avg_ms": 40.0, "rtt_min_ms": 30.0, "rtt_max_ms": 50.0},
            {"ts": h0 + 70, "interface": "wwan0", "target": "1.1.1.1",
             "sent": 5, "received": 4, "loss_pct": 20.0,
             "rtt_avg_ms": 60.0, "rtt_min_ms": 55.0, "rtt_max_ms": 70.0},
            {"ts": h0 + 90, "interface": "eth0", "target": "1.1.1.1",
             "sent": 5, "received": 5, "loss_pct": 0.0,
             "rtt_avg_ms": 8.0, "rtt_min_ms": 7.0, "rtt_max_ms": 9.0},
        ]
        rolls = agg.bucket(samples, "hour")
        wwan = next(r for r in rolls if r["interface"] == "wwan0")
        assert wwan["bucket_start"] == h0
        assert wwan["sent"] == 10 and wwan["received"] == 9
        assert wwan["loss_pct"] == 10.0
        # RTT weighted by received: (40*5 + 60*4) / 9
        assert wwan["rtt_avg_ms"] == round((40 * 5 + 60 * 4) / 9, 2)
        assert wwan["rtt_min_ms"] == 30.0 and wwan["rtt_max_ms"] == 70.0
        assert len(rolls) == 2  # wwan0 + eth0

    def test_total_loss_bucket_has_null_rtt(self):
        h0 = agg.bucket_start(1_000_000.0, "hour")
        samples = [{
            "ts": h0 + 5, "interface": "wwan0", "target": "8.8.8.8",
            "sent": 5, "received": 0, "loss_pct": 100.0,
            "rtt_avg_ms": None, "rtt_min_ms": None, "rtt_max_ms": None,
        }]
        roll = agg.bucket(samples, "hour")[0]
        assert roll["loss_pct"] == 100.0 and roll["rtt_avg_ms"] is None


class TestSummarize:
    def test_series_keyed_and_headline_weighted(self):
        rows = [
            {"ts": 100, "interface": "wwan0", "target": "1.1.1.1", "loss_pct": 0.0,
             "rtt_avg_ms": 40.0, "rtt_min_ms": 30.0, "rtt_max_ms": 50.0,
             "sent": 5, "received": 5},
            {"ts": 160, "interface": "wwan0", "target": "1.1.1.1", "loss_pct": 20.0,
             "rtt_avg_ms": 60.0, "rtt_min_ms": 55.0, "rtt_max_ms": 70.0,
             "sent": 5, "received": 4},
        ]
        out = agg.summarize_latency(rows, 0, 200)
        assert out["interfaces"] == ["wwan0"]
        assert list(out["series"].keys()) == ["wwan0|1.1.1.1"]
        assert len(out["series"]["wwan0|1.1.1.1"]) == 2
        hd = out["headline"]["wwan0"]
        assert hd["sent"] == 10 and hd["received"] == 9
        assert hd["loss_pct"] == 10.0


# ── PingMonitor iteration (fake pinger + in-memory DB) ───────────────────────

def _config(**over):
    from sim_monitor.config.schema import LatencyConfig
    base = dict(enabled=True, interval_seconds=60, targets=["1.1.1.1", "8.8.8.8"],
                packet_count=5, timeout_seconds=2)
    base.update(over)
    return LatencyConfig.model_validate(base)


def _monitor(db, pinger, interfaces, clock):
    store = StateStore()
    store.set_state(State.CONNECTED, interface="wwan0")
    return PingMonitor(
        store=store, db=db, events=EventLog(db),
        get_config=_config,
        pinger=pinger,
        list_interfaces=lambda: interfaces,
        monotonic=clock,
        wall_clock=clock,
    )


class TestPingMonitor:
    def test_probe_writes_sample_per_interface_target(self):
        db = Database(":memory:")
        t = [1_000_000.0]
        mon = _monitor(db, make_fake_pinger(), ["eth0", "wlan0"], lambda: t[0])
        cfg = _config()
        rows = mon.probe(cfg)
        # interfaces from list + cellular(wwan0) from store, × 2 targets
        ifaces = {r["interface"] for r in rows}
        assert ifaces == {"eth0", "wlan0", "wwan0"}
        assert len(rows) == 3 * 2
        stored = db.icmp_samples_between(0, 2_000_000)
        assert len(stored) == 6
        db.close()

    def test_total_loss_emits_event(self):
        db = Database(":memory:")
        t = [1_000_000.0]

        def dead_ping(host, interface=None, count=5, timeout=2):
            return {"sent": count, "received": 0, "loss_pct": 100.0,
                    "avg_ms": None, "min_ms": None, "max_ms": None}
        mon = _monitor(db, dead_ping, ["eth0"], lambda: t[0])
        mon.probe(_config())
        events = db.recent_events(kind="latency")
        assert any("100% packet loss" in e["message"] for e in events)
        db.close()

    def test_interval_gating(self):
        db = Database(":memory:")
        t = [1000.0]
        calls = []

        def counting(host, interface=None, count=5, timeout=2):
            calls.append(1)
            return {"sent": count, "received": count, "loss_pct": 0.0,
                    "avg_ms": 10.0, "min_ms": 9.0, "max_ms": 11.0}
        mon = _monitor(db, counting, ["eth0"], lambda: t[0])
        mon._iteration(forced=False)            # fires (first due)
        n1 = len(calls)
        mon._iteration(forced=False)            # within interval -> skipped
        assert len(calls) == n1
        t[0] += 61                               # past interval
        mon._iteration(forced=False)            # fires again
        assert len(calls) > n1
        db.close()

    def test_rollups_folded_for_completed_buckets(self):
        db = Database(":memory:")
        h0 = agg.bucket_start(1_000_000.0, "hour")
        # First cycle in hour h0, second cycle well into the *next* hour so h0
        # is complete and should be rolled up.
        clock = [h0 + 30]
        mon = _monitor(db, make_fake_pinger(), ["eth0"], lambda: clock[0])
        mon.probe(_config())
        clock[0] = h0 + 3600 + 30
        mon.probe(_config())
        rolls = db.icmp_rollups_between("hour", 0, 2_000_000)
        assert any(r["bucket_start"] == h0 for r in rolls)
        db.close()


class TestPayloadStats:
    def _row(self, now, dt, target, recv, avg):
        return {"ts": now - dt, "interface": "wwan0", "target": target,
                "sent": 5, "received": recv, "loss_pct": round((1 - recv / 5) * 100, 1),
                "rtt_avg_ms": avg, "rtt_min_ms": avg, "rtt_max_ms": avg}

    def test_last_and_windows(self):
        now = 1_000_000.0
        rows = [
            self._row(now, 30, "1.1.1.1", 5, 40.0),
            self._row(now, 30, "8.8.8.8", 5, 50.0),       # last cycle: avg 45, loss 0
            self._row(now, 2 * 3600, "1.1.1.1", 4, 80.0),  # 2h ago (in 3h/6h/24h, not 1h)
            self._row(now, 2 * 3600, "8.8.8.8", 5, 80.0),
        ]
        s = agg.payload_stats(rows, now)
        assert s["latency_ms"] == 45.0 and s["loss_pct"] == 0.0
        assert s["latency_min_ms"] == 40.0 and s["latency_max_ms"] == 50.0
        assert s["latency_1h"] == 45.0 and s["loss_1h"] == 0.0  # only last cycle
        assert s["loss_3h"] == 5.0                              # sent 20, recv 19
        assert s["latency_3h"] == round(1170 / 19, 2)           # received-weighted
        assert s["latency_min_3h"] == 40.0 and s["latency_max_3h"] == 80.0
        assert s["loss_24h"] == 5.0

    def test_prefix_applies_to_every_key(self):
        s = agg.payload_stats([], 1000.0, prefix="http_")
        assert all(k.startswith("http_") for k in s)
        assert "http_latency_min_ms" in s and "http_loss_24h" in s

    def test_empty_is_all_none(self):
        s = agg.payload_stats([], 1000.0)
        assert s["latency_ms"] is None and s["loss_24h"] is None
        assert all(v is None for v in s.values())
        # last cycle + 4 windows, each with avg/min/max latency + loss = 5 × 4 keys
        assert len(s) == 20
        assert {"latency_ms", "latency_min_ms", "latency_max_ms", "loss_pct"} <= set(s)
        assert {"latency_min_24h", "latency_max_1h"} <= set(s)


class TestLatencyPlaceholderContext:
    def test_reads_interface_samples(self):
        import time as _t

        from sim_monitor.monitor.http_monitor import latency_placeholder_context

        db = Database(":memory:")
        db.add_icmp_samples(_t.time() - 20, [
            {"interface": "wwan0", "target": "1.1.1.1", "sent": 5, "received": 5,
             "loss_pct": 0.0, "rtt_avg_ms": 42.0, "rtt_min_ms": 40.0, "rtt_max_ms": 44.0},
        ])
        ctx = latency_placeholder_context(db, "wwan0")
        assert ctx["latency_ms"] == 42.0 and ctx["loss_pct"] == 0.0
        assert ctx["latency_24h"] == 42.0
        assert latency_placeholder_context(db, "eth0")["latency_ms"] is None
        assert latency_placeholder_context(db, None)["latency_ms"] is None
        db.close()

    def test_falls_back_to_last_cellular_interface_when_degraded(self):
        import time as _t

        from sim_monitor.monitor.http_monitor import latency_placeholder_context

        db = Database(":memory:")
        db.set_setting("cellular_interface", "wwan0")  # recorded while connected
        db.add_icmp_samples(_t.time() - 20, [
            {"interface": "wwan0", "target": "1.1.1.1", "sent": 5, "received": 4,
             "loss_pct": 20.0, "rtt_avg_ms": 55.0, "rtt_min_ms": 50.0, "rtt_max_ms": 60.0},
        ])
        # interface is None (degraded) but stats still resolve via the fallback.
        ctx = latency_placeholder_context(db, None)
        assert ctx["latency_ms"] == 55.0 and ctx["loss_pct"] == 20.0
        db.close()


def test_fake_pinger_is_plausible():
    import random
    p = make_fake_pinger(rng=random.Random(7))
    r = p("1.1.1.1", interface="eth0", count=5)
    assert r["sent"] == 5
    assert 0 <= r["received"] <= 5
    if r["received"]:
        assert r["min_ms"] <= r["avg_ms"] <= r["max_ms"]


def test_trigger_defaults_when_omitted():
    db = Database(":memory:")
    store = StateStore()
    mon = PingMonitor(store=store, db=db, events=EventLog(db), get_config=_config)
    assert isinstance(mon.trigger, threading.Event)
    db.close()


class TestEffectiveConfig:
    def test_falls_back_to_default_when_unset(self):
        from sim_monitor.monitor.ping_monitor import effective_latency_config

        db = Database(":memory:")
        default = _config(enabled=False, interval_seconds=60)
        assert effective_latency_config(db, default) is default
        db.close()

    def test_db_setting_overrides_default(self):
        from sim_monitor.monitor.ping_monitor import effective_latency_config

        db = Database(":memory:")
        default = _config(enabled=False, interval_seconds=60)
        stored = _config(enabled=True, interval_seconds=120)
        db.set_setting("latency", stored.model_dump(mode="json"))
        eff = effective_latency_config(db, default)
        assert eff.enabled is True and eff.interval_seconds == 120
        db.close()

    def test_invalid_stored_config_falls_back(self):
        from sim_monitor.monitor.ping_monitor import effective_latency_config

        db = Database(":memory:")
        default = _config(enabled=True, interval_seconds=60)
        db.set_setting("latency", {"interval_seconds": 1})  # below ge=10 -> invalid
        assert effective_latency_config(db, default) is default
        db.close()

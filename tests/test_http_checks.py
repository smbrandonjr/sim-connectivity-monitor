"""Tests for the per-interface HTTP/website reachability monitor: probe routing
+ status/latency mapping, rollup folding, effective config, payload placeholders,
and schema validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sim_monitor.config.schema import HttpCheckConfig
from sim_monitor.core import latency as agg
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor.http_check_monitor import (
    HttpCheckMonitor,
    effective_http_check_config,
    make_fake_http_prober,
)
from sim_monitor.storage.db import Database


def _config(**over):
    base = dict(enabled=True, interval_seconds=60,
                targets=["https://google.com/generate_204"], timeout_seconds=10)
    base.update(over)
    return HttpCheckConfig.model_validate(base)


def _monitor(db, prober, interfaces, clock):
    store = StateStore()
    store.set_state(State.CONNECTED, interface="wwan0")
    return HttpCheckMonitor(
        store=store, db=db, events=EventLog(db), get_config=_config,
        prober=prober, list_interfaces=lambda: interfaces,
        monotonic=clock, wall_clock=clock,
    )


class TestHttpCheckMonitor:
    def test_success_records_status_and_latency(self):
        db = Database(":memory:")
        calls = []

        def ok(url, interface=None, timeout=10):
            calls.append((url, interface, timeout))
            return {"ok": True, "status": 204, "latency_ms": 123}

        mon = _monitor(db, ok, ["eth0"], lambda: 1_000_000.0)
        rows = mon.probe(_config())
        # eth0 (listed) + wwan0 (cellular from store) × 1 URL
        assert {r["interface"] for r in rows} == {"eth0", "wwan0"}
        assert {c[2] for c in calls} == {10}  # timeout passed through
        assert all(r["ok"] == 1 and r["status_code"] == 204 for r in rows)
        assert all(r["latency_ms"] == 123 for r in rows)
        stored = db.http_samples_between(0, 2_000_000)
        assert len(stored) == 2
        db.close()

    def test_4xx_5xx_is_failure_with_status_kept(self):
        db = Database(":memory:")

        def err(url, interface=None, timeout=10):
            return {"ok": True, "status": 503, "latency_ms": 88}

        mon = _monitor(db, err, ["eth0"], lambda: 1_000_000.0)
        rows = mon.probe(_config())
        # failure: not received, no latency, but the status code is still recorded
        assert all(r["ok"] == 0 and r["latency_ms"] is None for r in rows)
        assert all(r["status_code"] == 503 for r in rows)
        db.close()

    def test_timeout_is_failure_no_status(self):
        db = Database(":memory:")

        def dead(url, interface=None, timeout=10):
            return {"ok": False, "status": None, "latency_ms": 10000, "error": "timeout"}

        mon = _monitor(db, dead, ["eth0"], lambda: 1_000_000.0)
        rows = mon.probe(_config())
        assert all(r["ok"] == 0 and r["status_code"] is None for r in rows)
        events = db.recent_events(kind="http_check")
        assert any("all web checks failed" in e["message"] for e in events)
        db.close()

    def test_rollups_folded_with_representative_status(self):
        db = Database(":memory:")
        h0 = agg.bucket_start(1_000_000.0, "hour")
        clock = [h0 + 30]

        def ok(url, interface=None, timeout=10):
            return {"ok": True, "status": 204, "latency_ms": 50}

        mon = _monitor(db, ok, ["eth0"], lambda: clock[0])
        mon.probe(_config())
        clock[0] = h0 + 3600 + 30  # next hour completes h0
        mon.probe(_config())
        rolls = db.http_rollups_between("hour", 0, 2_000_000)
        h0_rolls = [r for r in rolls if r["bucket_start"] == h0]
        assert h0_rolls and all(r["status_code"] == 204 for r in h0_rolls)
        db.close()


class TestEffectiveConfig:
    def test_falls_back_to_default_when_unset(self):
        db = Database(":memory:")
        default = HttpCheckConfig(enabled=True, targets=["https://example.com"])
        assert effective_http_check_config(db, default).targets == ["https://example.com"]
        db.close()

    def test_db_setting_overrides_default(self):
        db = Database(":memory:")
        db.set_setting("http_checks", {"enabled": True, "interval_seconds": 30,
                                       "targets": ["https://a.example"]})
        eff = effective_http_check_config(db, HttpCheckConfig())
        assert eff.interval_seconds == 30 and eff.targets == ["https://a.example"]
        db.close()

    def test_invalid_stored_config_falls_back(self):
        db = Database(":memory:")
        db.set_setting("http_checks", {"targets": ["not-a-url"]})  # fails URL validation
        default = HttpCheckConfig(enabled=True)
        assert effective_http_check_config(db, default) is default
        db.close()


class TestSchema:
    def test_rejects_non_url_target(self):
        with pytest.raises(ValidationError):
            HttpCheckConfig(targets=["1.1.1.1"])

    def test_accepts_http_and_https(self):
        cfg = HttpCheckConfig(targets=["http://x.example", "https://y.example/204"])
        assert len(cfg.targets) == 2


class TestPayloadStats:
    def test_http_prefixed_keys(self):
        from sim_monitor.core.latency import http_sample_to_metric, payload_stats

        now = 1_000_000.0
        samples = [
            {"ts": now - 10, "interface": "wwan0", "target": "https://a",
             "ok": 1, "status_code": 204, "latency_ms": 120},
            {"ts": now - 10, "interface": "wwan0", "target": "https://b",
             "ok": 0, "status_code": 500, "latency_ms": None},
        ]
        out = payload_stats([http_sample_to_metric(s) for s in samples], now, prefix="http_")
        assert "http_latency_ms" in out and "http_loss_pct" in out
        assert "http_latency_24h" in out and "http_loss_24h" in out
        # one of two checks failed -> 50% loss in the latest cycle
        assert out["http_loss_pct"] == 50.0
        assert out["http_latency_ms"] == 120  # only the successful one counts


def test_fake_http_prober_is_plausible():
    import random

    p = make_fake_http_prober(rng=random.Random(3))
    res = p("https://x", interface="eth0")
    assert "ok" in res and "latency_ms" in res

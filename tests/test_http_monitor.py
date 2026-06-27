import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import requests

from sim_monitor.config.schema import MonitorConfig, MonitorSchedule
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor import http_monitor
from sim_monitor.monitor.http_monitor import HttpMonitor
from sim_monitor.storage.db import Database


def monitor_cfg(*, body=None, body_fields=None, egress="cellular", **dest_over):
    """Build a one-destination MonitorConfig (shared payload + one endpoint)."""
    dest = {
        "egress": egress, "method": "POST",
        "url": "https://hooks.example.com/hb/{iccid}",
        "headers": {"X-IMEI": "{imei}"}, "interval_seconds": 60,
        "expect_status": [200, 204],
    }
    dest.update(dest_over)
    cfg: dict = {"enabled": True, "destinations": [dest]}
    if body_fields is not None:
        cfg["body_fields"] = body_fields
    else:
        cfg["body"] = body if body is not None else '{"ip":"{ip_address}"}'
    return MonitorConfig.model_validate(cfg)


# Default config used by most tests (single cellular destination).
CFG = monitor_cfg()


def fire(monitor, cfg, idx=0):
    """Send the cfg's destination through probe() (bypassing scheduling)."""
    return monitor.probe(cfg, cfg.destinations[idx])


@dataclass
class StubResponse:
    status_code: int


class StubSession:
    def __init__(self, status_code=200, exc=None):
        self.status_code = status_code
        self.exc = exc
        self.calls = []

    def request(self, method, url, headers=None, data=None, timeout=None):
        self.calls.append({"method": method, "url": url, "headers": headers, "data": data})
        if self.exc:
            raise self.exc
        return StubResponse(self.status_code)


@pytest.fixture
def env(monkeypatch):
    store = StateStore()
    store.set_state(
        State.CONNECTED,
        iccid="8944500612345678901",
        imei="490154203237518",
        ip_address="10.1.2.3",
        interface="wwan0",
    )
    db = Database(":memory:")
    session = StubSession()
    session.bound_interfaces = []

    def fake_make_session(interface=None):
        session.bound_interfaces.append(interface)
        return session

    monkeypatch.setattr(http_monitor, "make_session", fake_make_session)
    monitor = HttpMonitor(
        store=store,
        db=db,
        events=EventLog(db),
        get_config=lambda: CFG,
        trigger=threading.Event(),
        list_interfaces=lambda: [],  # no Wi-Fi by default (deterministic)
    )
    return monitor, session, db


def test_probe_renders_placeholders_and_records_ok(env):
    monitor, session, db = env
    assert fire(monitor, CFG) is True
    call = session.calls[0]
    assert call["url"] == "https://hooks.example.com/hb/8944500612345678901"
    assert call["headers"]["X-IMEI"] == "490154203237518"
    assert call["data"] == b'{"ip":"10.1.2.3"}'
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 1
    assert result["status_code"] == 200
    assert result["interface"] == "wwan0"  # egress recorded


def test_probe_unexpected_status_recorded_as_failure(env):
    monitor, session, db = env
    session.status_code = 500
    assert fire(monitor, CFG) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "500" in result["error"]


def test_probe_network_error_recorded(env):
    monitor, session, db = env
    session.exc = requests.ConnectionError("no route to host")
    assert fire(monitor, CFG) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "no route" in result["error"]


def test_egress_cellular_binds_cellular(env):
    monitor, session, _ = env
    fire(monitor, monitor_cfg(egress="cellular"))
    assert session.bound_interfaces == ["wwan0"]


def test_egress_wlan_binds_wlan(env):
    monitor, session, _ = env
    monitor.list_interfaces = lambda: ["eth0", "wlan0", "wwan0"]
    fire(monitor, monitor_cfg(egress="wlan"))
    assert session.bound_interfaces == ["wlan0"]


def test_egress_wlan_falls_back_to_os_routing_when_no_wifi(env):
    monitor, session, _ = env  # fixture injects no interfaces
    fire(monitor, monitor_cfg(egress="wlan"))
    assert session.bound_interfaces == [None]


def test_egress_auto_unbound_for_lan_endpoints(env):
    monitor, session, _ = env
    fire(monitor, monitor_cfg(egress="auto"))
    assert session.bound_interfaces == [None]


def test_egress_interface_placeholder_in_payload(env):
    monitor, session, _ = env
    cfg = monitor_cfg(egress="cellular", body='{"path":"{egress_interface}"}')
    fire(monitor, cfg)
    assert session.calls[0]["data"].decode() == '{"path":"wwan0"}'


def test_egress_interface_empty_when_os_routed(env):
    monitor, session, _ = env
    cfg = monitor_cfg(egress="auto", body='{"path":"{egress_interface}"}')
    fire(monitor, cfg)
    assert session.calls[0]["data"].decode() == '{"path":""}'


def test_multiple_destinations_each_over_its_interface(env):
    """The shared payload is delivered to each destination over its own egress."""
    monitor, session, db = env
    monitor.list_interfaces = lambda: ["wlan0", "wwan0"]
    cfg = MonitorConfig.model_validate({
        "enabled": True,
        "body": '{"path":"{egress_interface}"}',
        "destinations": [
            {"egress": "wlan", "url": "http://10.0.0.5/hb", "interval_seconds": 60},
            {"egress": "cellular", "url": "https://api.example/hb", "interval_seconds": 900},
        ],
    })
    monitor.get_config = lambda: cfg
    monitor._iteration(forced=True)  # fire both now
    sent = {c["url"]: c["data"].decode() for c in session.calls}
    assert sent["http://10.0.0.5/hb"] == '{"path":"wlan0"}'
    assert sent["https://api.example/hb"] == '{"path":"wwan0"}'
    assert {r["interface"] for r in db.recent_monitor_results(5)} == {"wlan0", "wwan0"}


def test_disabled_destination_skipped(env):
    monitor, session, _ = env
    cfg = MonitorConfig.model_validate({
        "enabled": True,
        "body": "{}",
        "destinations": [
            {"egress": "auto", "url": "https://on/", "enabled": True, "interval_seconds": 60},
            {"egress": "auto", "url": "https://off/", "enabled": False, "interval_seconds": 60},
        ],
    })
    monitor.get_config = lambda: cfg
    monitor._iteration(forced=True)
    assert [c["url"] for c in session.calls] == ["https://on/"]


def test_destination_holds_until_its_interval(env):
    """A destination fires once, then not again until its interval elapses."""
    monitor, session, _ = env
    cfg = monitor_cfg(egress="auto", url="https://x/", interval_seconds=600)
    monitor.get_config = lambda: cfg
    monitor._iteration(forced=False)          # first fire
    assert len(session.calls) == 1
    monitor._iteration(forced=False)          # immediately again: not due yet
    assert len(session.calls) == 1


def test_shortening_interval_takes_effect_immediately(env):
    """Lowering the interval pulls the next send earlier (due derives from the
    current interval, not a deadline frozen at the last send)."""
    import time as _t

    monitor, session, _ = env
    cfg = monitor_cfg(egress="auto", url="https://x/", interval_seconds=600)
    monitor.get_config = lambda: cfg
    monitor._iteration(forced=False)          # first fire, records last-sent
    assert len(session.calls) == 1
    # Pretend the last send was 90s ago; with the long interval it's not due.
    key = next(iter(monitor._last_sent))
    monitor._last_sent[key] = _t.monotonic() - 90
    monitor._iteration(forced=False)
    assert len(session.calls) == 1
    # Shorten to 60s -> 90s since last send is now past due -> fires right away.
    cfg.destinations[0].interval_seconds = 60
    monitor._iteration(forced=False)
    assert len(session.calls) == 2


STATUS_CFG = monitor_cfg(
    egress="auto",
    url="https://hooks.example.com/hb",
    headers={},
    body='{"status":"{status}","msg":"{status_message}","iccid":"{iccid}"}',
)


def test_degraded_probe_goes_unbound_with_status_payload(env):
    monitor, session, _ = env
    monitor.store.set_state(State.DEGRADED, last_error="connection lost")
    assert fire(monitor, STATUS_CFG) is True
    assert session.bound_interfaces == [None]  # egress=auto -> any working route
    body = session.calls[0]["data"].decode()
    assert '"status":"degraded"' in body
    assert '"msg":"recovery in progress: connection lost"' in body


def test_connected_probe_reports_connected_status(env):
    monitor, session, _ = env
    monitor.store.update(operator="Hologram")
    fire(monitor, STATUS_CFG)
    body = session.calls[0]["data"].decode()
    assert '"status":"connected"' in body
    assert "via Hologram" in body


FIELDS_CFG = monitor_cfg(
    url="https://hooks.example.com/ingest",
    headers={},
    body_fields=[
        {"path": "iccid", "value": "iccid"},
        {"path": "status", "value": "status"},
        {"path": "signal.rssi_dbm", "value": "rssi"},
        {"path": "signal.sinr_db", "value": "sinr"},  # unknown -> omitted
        {"path": "meta.tags", "value": "warehouse", "kind": "static"},
    ],
)


def test_body_fields_produce_valid_typed_json(env):
    import json

    monitor, session, _ = env
    monitor.store.update(signal_rssi=-67)
    fire(monitor, FIELDS_CFG)
    sent = json.loads(session.calls[0]["data"].decode())
    assert sent["iccid"] == "8944500612345678901"
    assert sent["status"] == "connected"
    assert sent["signal"]["rssi_dbm"] == -67  # native int
    assert "sinr_db" not in sent["signal"]  # unknown omitted -> still valid JSON
    assert sent["meta"]["tags"] == "warehouse"


def test_public_ip_fetched_bound_to_interface(env):
    monitor, session, _ = env
    session.status_code = 200

    class IpResp:
        ok = True
        text = "203.0.113.45\n"

    session.request = lambda *a, **k: None  # unused
    session.get = lambda url, timeout=None: IpResp()
    monitor._maybe_public_ip()
    assert monitor.store.get().public_ip == "203.0.113.45"
    assert session.bound_interfaces[-1] == "wwan0"  # bound to cellular


def test_public_ip_skipped_when_not_connected(env):
    monitor, session, _ = env
    monitor.store.set_state(State.DEGRADED)
    monitor._next_public_ip = 0
    called = []
    session.get = lambda *a, **k: called.append(1)
    monitor._maybe_public_ip()
    assert called == []


def test_send_when_degraded_default_on():
    assert MonitorConfig().send_when_degraded is True


def test_egress_default_is_wlan():
    d = MonitorConfig.model_validate({"destinations": [{"url": "https://x/"}]}).destinations[0]
    assert d.egress == "wlan"


def test_legacy_single_request_migrates_to_destination():
    """An old single-endpoint config folds into one destination + shared body."""
    legacy = MonitorConfig.model_validate({
        "enabled": True,
        "interval_seconds": 120,
        "bind_cellular": True,
        "request": {
            "method": "POST", "url": "https://old/hb",
            "headers": {"A": "b"}, "body": '{"x":1}', "expect_status": [200],
        },
    })
    assert legacy.body == '{"x":1}'
    assert len(legacy.destinations) == 1
    d = legacy.destinations[0]
    assert d.url == "https://old/hb" and d.egress == "cellular"
    assert d.interval_seconds == 120 and d.expect_status == [200]


class TestPause:
    def test_paused_holds_scheduled_sends(self, env):
        monitor, session, _ = env
        monitor.store.update(monitor_paused=True)
        monitor._iteration(forced=False)
        assert session.calls == []
        assert monitor._last_sent == {}  # schedule held, fires promptly on resume

    def test_manual_send_works_while_paused(self, env):
        monitor, session, _ = env
        monitor.store.update(monitor_paused=True)
        monitor._iteration(forced=True)
        assert len(session.calls) == 1

    def test_resume_fires_on_next_iteration(self, env):
        monitor, session, _ = env
        monitor.store.update(monitor_paused=True)
        monitor._iteration(forced=False)
        monitor.store.update(monitor_paused=False)
        monitor._iteration(forced=False)
        assert len(session.calls) == 1


class TestSchedule:
    WED_IN = datetime(2025, 6, 11, 14, 0, tzinfo=UTC)   # Wed 10:00 EDT
    SUN_OUT = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)  # Sun 10:00 EDT

    def _scheduled(self, **kw):
        cfg = CFG.model_copy(deep=True)
        cfg.destinations[0].schedule = MonitorSchedule(enabled=True, **kw)
        return cfg

    def test_scheduled_probe_fires_inside_window(self, env):
        monitor, session, _ = env
        cfg = self._scheduled()
        monitor.get_config = lambda: cfg
        monitor._wall_clock = lambda: self.WED_IN
        monitor._iteration(forced=False)
        assert len(session.calls) == 1

    def test_scheduled_probe_skipped_outside_window(self, env):
        monitor, session, _ = env
        cfg = self._scheduled()
        monitor.get_config = lambda: cfg
        monitor._wall_clock = lambda: self.SUN_OUT
        monitor._iteration(forced=False)
        assert session.calls == []
        assert monitor._last_sent == {}  # schedule held; fires when window opens

    def test_manual_send_bypasses_schedule(self, env):
        monitor, session, _ = env
        cfg = self._scheduled(override="off")  # even a hard off
        monitor.get_config = lambda: cfg
        monitor._wall_clock = lambda: self.SUN_OUT
        monitor._iteration(forced=True)
        assert len(session.calls) == 1

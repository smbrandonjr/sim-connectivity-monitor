import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
import requests

from sim_monitor.config.schema import MonitorSchedule, Profile
from sim_monitor.core.events import EventLog
from sim_monitor.core.state_store import StateStore
from sim_monitor.core.states import State
from sim_monitor.monitor import http_monitor
from sim_monitor.monitor.http_monitor import HttpMonitor
from sim_monitor.storage.db import Database

PROFILE = Profile.model_validate(
    {
        "name": "monitored",
        "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
        "monitor": {
            "enabled": True,
            "interval_seconds": 60,
            "request": {
                "method": "POST",
                "url": "https://hooks.example.com/hb/{iccid}",
                "headers": {"X-IMEI": "{imei}"},
                "body": '{"ip":"{ip_address}"}',
                "expect_status": [200, 204],
            },
        },
    }
)


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
        get_config=lambda: PROFILE.monitor,
        trigger=threading.Event(),
        list_interfaces=lambda: [],  # no Wi-Fi by default (deterministic)
    )
    return monitor, session, db


def test_probe_renders_placeholders_and_records_ok(env):
    monitor, session, db = env
    assert monitor.probe(PROFILE.monitor) is True
    call = session.calls[0]
    assert call["url"] == "https://hooks.example.com/hb/8944500612345678901"
    assert call["headers"]["X-IMEI"] == "490154203237518"
    assert call["data"] == b'{"ip":"10.1.2.3"}'
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 1
    assert result["status_code"] == 200


def test_probe_unexpected_status_recorded_as_failure(env):
    monitor, session, db = env
    session.status_code = 500
    assert monitor.probe(PROFILE.monitor) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "500" in result["error"]


def test_probe_network_error_recorded(env):
    monitor, session, db = env
    session.exc = requests.ConnectionError("no route to host")
    assert monitor.probe(PROFILE.monitor) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "no route" in result["error"]


def test_egress_cellular_binds_cellular(env):
    monitor, session, _ = env
    cell = PROFILE.monitor.model_copy(deep=True)
    cell.egress = "cellular"
    monitor.probe(cell)
    assert session.bound_interfaces == ["wwan0"]


def test_egress_wlan_binds_wlan(env):
    monitor, session, _ = env
    monitor.list_interfaces = lambda: ["eth0", "wlan0", "wwan0"]
    # PROFILE defaults to egress="wlan"
    monitor.probe(PROFILE.monitor)
    assert session.bound_interfaces == ["wlan0"]


def test_egress_wlan_falls_back_to_os_routing_when_no_wifi(env):
    monitor, session, _ = env  # fixture injects no interfaces
    monitor.probe(PROFILE.monitor)  # egress="wlan", but no wlan present
    assert session.bound_interfaces == [None]


def test_egress_auto_unbound_for_lan_endpoints(env):
    monitor, session, _ = env
    lan = PROFILE.monitor.model_copy(deep=True)
    lan.egress = "auto"
    monitor.probe(lan)
    assert session.bound_interfaces == [None]  # routed normally (LAN reachable)


def test_legacy_bind_cellular_migrates_to_egress():
    from sim_monitor.config.schema import MonitorConfig

    assert MonitorConfig.model_validate({"bind_cellular": True}).egress == "cellular"
    assert MonitorConfig.model_validate({"bind_cellular": False}).egress == "auto"
    assert MonitorConfig().egress == "wlan"  # new default


STATUS_PROFILE = Profile.model_validate(
    {
        "name": "status-monitored",
        "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
        "monitor": {
            "enabled": True,
            "interval_seconds": 60,
            "request": {
                "method": "POST",
                "url": "https://hooks.example.com/hb",
                "body": '{"status":"{status}","msg":"{status_message}","iccid":"{iccid}"}',
            },
        },
    }
)


def test_degraded_probe_goes_unbound_with_status_payload(env):
    monitor, session, db = env
    # Cellular went down: daemon is in DEGRADED, interface info is stale.
    monitor.store.set_state(State.DEGRADED, last_error="connection lost")
    assert monitor.probe(STATUS_PROFILE.monitor) is True
    assert session.bound_interfaces == [None]  # any working route (eth/wlan)
    body = session.calls[0]["data"].decode()
    assert '"status":"degraded"' in body
    assert '"msg":"recovery in progress: connection lost"' in body


def test_connected_probe_reports_connected_status(env):
    monitor, session, _ = env
    monitor.store.update(operator="Hologram")
    monitor.probe(STATUS_PROFILE.monitor)
    body = session.calls[0]["data"].decode()
    assert '"status":"connected"' in body
    assert "via Hologram" in body


FIELDS_PROFILE = Profile.model_validate(
    {
        "name": "fields",
        "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}],
        "monitor": {
            "enabled": True,
            "request": {
                "method": "POST",
                "url": "https://hooks.example.com/ingest",
                "body_fields": [
                    {"path": "iccid", "value": "iccid"},
                    {"path": "status", "value": "status"},
                    {"path": "signal.rssi_dbm", "value": "rssi"},
                    {"path": "signal.sinr_db", "value": "sinr"},  # unknown -> omitted
                    {"path": "meta.tags", "value": "warehouse", "kind": "static"},
                ],
            },
        },
    }
)


def test_body_fields_produce_valid_typed_json(env):
    import json

    monitor, session, _ = env
    monitor.store.update(signal_rssi=-67)
    monitor.probe(FIELDS_PROFILE.monitor)
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
    assert PROFILE.monitor.send_when_degraded is True


class TestPause:
    def test_paused_holds_scheduled_sends(self, env):
        monitor, session, _ = env
        monitor.get_config = lambda: PROFILE.monitor
        monitor.store.update(monitor_paused=True)
        monitor._iteration(forced=False)
        assert session.calls == []
        assert monitor._next_due is None  # schedule held, fires promptly on resume

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
        cfg = PROFILE.monitor.model_copy(deep=True)
        cfg.schedule = MonitorSchedule(enabled=True, **kw)
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
        assert monitor._next_due is None  # schedule held; fires when window opens

    def test_manual_send_bypasses_schedule(self, env):
        monitor, session, _ = env
        cfg = self._scheduled(override="off")  # even a hard off
        monitor.get_config = lambda: cfg
        monitor._wall_clock = lambda: self.SUN_OUT
        monitor._iteration(forced=True)
        assert len(session.calls) == 1

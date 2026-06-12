import threading
from dataclasses import dataclass

import pytest
import requests

from sim_monitor.config.schema import Profile
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
        get_profile=lambda: PROFILE,
        trigger=threading.Event(),
    )
    return monitor, session, db


def test_probe_renders_placeholders_and_records_ok(env):
    monitor, session, db = env
    assert monitor.probe(PROFILE) is True
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
    assert monitor.probe(PROFILE) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "500" in result["error"]


def test_probe_network_error_recorded(env):
    monitor, session, db = env
    session.exc = requests.ConnectionError("no route to host")
    assert monitor.probe(PROFILE) is False
    result = db.recent_monitor_results(1)[0]
    assert result["ok"] == 0
    assert "no route" in result["error"]


def test_probe_binds_cellular_while_connected(env):
    monitor, session, _ = env
    monitor.probe(PROFILE)
    assert session.bound_interfaces == ["wwan0"]


def test_bind_cellular_false_for_lan_endpoints(env):
    monitor, session, _ = env
    lan_profile = PROFILE.model_copy(deep=True)
    lan_profile.monitor.bind_cellular = False
    monitor.probe(lan_profile)
    assert session.bound_interfaces == [None]  # routed normally (LAN reachable)


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
    assert monitor.probe(STATUS_PROFILE) is True
    assert session.bound_interfaces == [None]  # any working route (eth/wlan)
    body = session.calls[0]["data"].decode()
    assert '"status":"degraded"' in body
    assert '"msg":"recovery in progress: connection lost"' in body


def test_connected_probe_reports_connected_status(env):
    monitor, session, _ = env
    monitor.store.update(operator="Hologram")
    monitor.probe(STATUS_PROFILE)
    body = session.calls[0]["data"].decode()
    assert '"status":"connected"' in body
    assert "via Hologram" in body


def test_send_when_degraded_default_on():
    assert PROFILE.monitor.send_when_degraded is True

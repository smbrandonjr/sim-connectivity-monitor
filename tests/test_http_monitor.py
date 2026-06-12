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
    monkeypatch.setattr(http_monitor, "make_session", lambda interface=None: session)
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

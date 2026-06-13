"""Web layer tests: the JSON API (which the SPA consumes) + SPA serving,
against the simulated backend (daemon ticked manually, no threads)."""

import pytest

from sim_monitor import app as app_module
from sim_monitor.config.loader import load_profiles
from sim_monitor.config.schema import AppConfig
from sim_monitor.core import commands as cmd
from sim_monitor.core.states import State
from sim_monitor.web.server import create_app

PROFILE_YAML = """\
name: web-test
match:
  iccid_patterns: ["*"]
  priority: 1000
pdp_contexts:
  - cid: 1
    apn: hologram
    bearer: true
"""


@pytest.fixture
def sim(tmp_path):
    profiles_dir = tmp_path / "profiles.d"
    profiles_dir.mkdir()
    (profiles_dir / "00-default.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    config = AppConfig.model_validate(
        {
            "simulate": True,
            "db_path": str(tmp_path / "test.db"),
            "profiles_dir": str(profiles_dir),
        }
    )
    profiles, errors = load_profiles(profiles_dir)
    assert not errors
    built = app_module.build(config, profiles)
    yield built
    built.db.close()


@pytest.fixture
def client(sim):
    flask_app = create_app(sim)
    flask_app.config["TESTING"] = True
    return flask_app.test_client()


def tick_until_connected(sim, max_ticks=25):
    for _ in range(max_ticks):
        sim.daemon.tick()
        if sim.daemon.state is State.CONNECTED:
            return
    raise AssertionError(f"never connected (state={sim.daemon.state})")


class TestSpaServing:
    def test_index_served(self, sim, client):
        page = client.get("/")
        assert page.status_code == 200
        assert b'id="app"' in page.data  # the SPA mount point


class TestStatusJson:
    def test_status(self, sim, client):
        tick_until_connected(sim)
        data = client.get("/api/status.json").get_json()
        assert data["state"] == "CONNECTED"
        assert data["iccid"] == "8944500612345678901"
        assert data["ip_address"] == "10.170.42.7"
        assert data["active_profile"] == "web-test"
        assert data["firmware"] == "FM100R-FAKE-01.001.01"
        assert "sms_pending" in data


class TestTimelineAndBundle:
    def test_timeline_json(self, sim, client):
        tick_until_connected(sim)
        rows = client.get("/api/timeline.json").get_json()
        assert any(r["source"] == "event" for r in rows)

    def test_bundle_downloads_with_filename(self, sim, client):
        tick_until_connected(sim)
        resp = client.get("/api/bundle.json")
        assert resp.status_code == 200
        assert "attachment" in resp.headers["Content-Disposition"]
        data = resp.get_json()
        assert data["schema"] == "sim-monitor/diagnostic-bundle@1"
        assert data["modem"]["firmware"] == "FM100R-FAKE-01.001.01"
        assert data["active_profile"]["name"] == "web-test"

    def test_urcs_and_identity_json(self, sim, client):
        tick_until_connected(sim)
        sim.daemon.driver.ota_swap("8946420000000000123")
        sim.daemon.tick()
        assert client.get("/api/urcs.json").status_code == 200
        identity = client.get("/api/identity.json").get_json()
        assert any(row["reason"] == "ota-swap" for row in identity)

    def test_events_and_monitor_json(self, sim, client):
        tick_until_connected(sim)
        assert client.get("/api/events.json").status_code == 200
        assert client.get("/api/monitor.json").status_code == 200


class TestTelemetryJson:
    def test_telemetry(self, sim, client):
        tick_until_connected(sim)
        sim.daemon.tick()
        data = client.get("/api/telemetry.json").get_json()
        assert data["latest"].get("rsrp") == -94
        assert isinstance(data["history"], list)


class TestSmsApi:
    def test_incoming_message_in_api(self, sim, client):
        tick_until_connected(sim)
        sim.daemon.driver.receive_sms("+12025550123", "field test message")
        sim.daemon.tick()
        data = client.get("/api/sms.json").get_json()
        assert any(m["body"] == "field test message" for m in data)


class TestJsonCommandApi:
    def test_simple_command(self, sim, client):
        resp = client.post("/api/cmd/reconnect")
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
        assert cmd.Reconnect() in sim.commands.drain()

    def test_fallback_test_with_duration(self, sim, client):
        client.post("/api/cmd/fallback-test", json={"duration_seconds": 120})
        assert cmd.StartFallbackTest(duration_seconds=120) in sim.commands.drain()

    def test_send_sms(self, sim, client):
        client.post("/api/cmd/send-sms", json={"number": "+1", "text": "hi"})
        assert cmd.SendSms(number="+1", text="hi") in sim.commands.drain()

    def test_delete_and_clear_sms(self, sim, client):
        client.post("/api/cmd/delete-sms", json={"row_id": 5})
        client.post("/api/cmd/clear-sms")
        drained = sim.commands.drain()
        assert cmd.DeleteSms(row_id=5) in drained
        assert cmd.ClearSms() in drained

    def test_run_diagnostics_rejects_non_at(self, sim, client):
        resp = client.post("/api/cmd/run-diagnostics", json={"commands": ["rm -rf /"]})
        assert resp.status_code == 400

    def test_run_diagnostics_ok(self, sim, client):
        client.post("/api/cmd/run-diagnostics", json={"commands": ["AT+CSQ"]})
        assert cmd.RunDiagnostics(commands=("AT+CSQ",)) in sim.commands.drain()

    def test_force_and_release(self, sim, client):
        client.post("/api/cmd/force-profile", json={"name": "web-test"})
        client.post("/api/cmd/release-force")
        drained = sim.commands.drain()
        assert cmd.ForceProfile(name="web-test") in drained
        assert cmd.ReleaseForce() in drained

    def test_pause_resume(self, sim, client):
        client.post("/api/cmd/monitor-pause")
        client.post("/api/cmd/monitor-resume")
        drained = sim.commands.drain()
        assert cmd.PauseMonitor() in drained
        assert cmd.ResumeMonitor() in drained

    def test_set_sim_name(self, sim, client):
        client.post("/api/cmd/set-sim-name", json={"name": "Lab Pi"})
        assert cmd.SetSimName(name="Lab Pi") in sim.commands.drain()

    def test_sim_name_in_status(self, sim, client):
        tick_until_connected(sim)
        sim.commands.put(cmd.SetSimName(name="Roof Unit"))
        sim.daemon.tick()
        assert client.get("/api/status.json").get_json()["sim_name"] == "Roof Unit"

    def test_unknown_command_404(self, sim, client):
        assert client.post("/api/cmd/nope").status_code == 404

    def test_force_profile_missing_arg_400(self, sim, client):
        assert client.post("/api/cmd/force-profile", json={}).status_code == 400

    def test_update_unavailable_in_simulate(self, sim, client):
        resp = client.post("/api/cmd/update-app")
        assert resp.status_code == 400
        assert "simulate" in resp.get_json()["error"]


class TestJsonProfileApi:
    def test_list(self, sim, client):
        data = client.get("/api/profiles.json").get_json()
        assert any(p["name"] == "web-test" for p in data["profiles"])

    def test_get_raw(self, sim, client):
        data = client.get("/api/profiles/web-test.json").get_json()
        assert "pdp_contexts" in data["yaml"]

    def test_create_update_delete(self, sim, client):
        new_yaml = (
            "name: created\nmatch: {iccid_patterns: ['8944111*'], priority: 10}\n"
            "pdp_contexts: [{cid: 1, apn: hologram, bearer: true}]\n"
        )
        assert client.post("/api/profiles", json={"yaml": new_yaml}).status_code == 200
        edited = new_yaml.replace("apn: hologram", "apn: hologram2")
        assert client.put("/api/profiles/created", json={"yaml": edited}).status_code == 200
        assert client.delete("/api/profiles/created").status_code == 200
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert {p.name for p in profiles} == {"web-test"}

    def test_create_invalid_400(self, sim, client):
        assert client.post("/api/profiles", json={"yaml": "name: x"}).status_code == 400

    def test_create_duplicate_400(self, sim, client):
        resp = client.post("/api/profiles", json={"yaml": PROFILE_YAML})
        assert resp.status_code == 400

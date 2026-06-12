"""Flask routes against the simulated backend (daemon ticked manually, no threads)."""

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


class TestDashboard:
    def test_dashboard_renders(self, sim, client):
        tick_until_connected(sim)
        page = client.get("/")
        assert page.status_code == 200
        assert b"CONNECTED" in page.data
        assert b"8944500612345678901" in page.data

    def test_status_json(self, sim, client):
        tick_until_connected(sim)
        data = client.get("/api/status.json").get_json()
        assert data["state"] == "CONNECTED"
        assert data["iccid"] == "8944500612345678901"
        assert data["ip_address"] == "10.170.42.7"
        assert data["active_profile"] == "web-test"


class TestActions:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("/actions/reconnect", cmd.Reconnect()),
            ("/actions/reset-modem", cmd.ResetModem()),
            ("/actions/monitor-now", cmd.RunMonitorNow()),
            ("/actions/fallback-abort", cmd.AbortFallbackTest()),
        ],
    )
    def test_action_enqueues_command(self, sim, client, path, expected):
        response = client.post(path)
        assert response.status_code == 302
        assert expected in sim.commands.drain()

    def test_fallback_test_with_duration(self, sim, client):
        response = client.post("/actions/fallback-test", data={"duration_seconds": "120"})
        assert response.status_code == 302
        assert cmd.StartFallbackTest(duration_seconds=120) in sim.commands.drain()

    def test_fallback_test_default_duration(self, sim, client):
        client.post("/actions/fallback-test", data={})
        assert cmd.StartFallbackTest(duration_seconds=None) in sim.commands.drain()

    def test_fallback_test_invalid_duration(self, sim, client):
        client.post("/actions/fallback-test", data={"duration_seconds": "abc"})
        assert sim.commands.drain() == []


class TestProfilesCrud:
    def test_list_profiles(self, sim, client):
        page = client.get("/profiles/")
        assert page.status_code == 200
        assert b"web-test" in page.data

    def test_create_profile(self, sim, client):
        new_yaml = PROFILE_YAML.replace("web-test", "created").replace('["*"]', '["8944111*"]')
        response = client.post("/profiles/new", data={"yaml_text": new_yaml})
        assert response.status_code == 302
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert {p.name for p in profiles} == {"web-test", "created"}
        assert cmd.ReloadProfiles() in sim.commands.drain()

    def test_create_duplicate_rejected(self, sim, client):
        response = client.post("/profiles/new", data={"yaml_text": PROFILE_YAML})
        assert response.status_code == 400
        assert b"already exists" in response.data

    def test_create_invalid_yaml_rejected(self, sim, client):
        response = client.post("/profiles/new", data={"yaml_text": "name: [unclosed"})
        assert response.status_code == 400

    def test_create_invalid_profile_rejected(self, sim, client):
        response = client.post(
            "/profiles/new", data={"yaml_text": "name: x\npdp_contexts: []\n"}
        )
        assert response.status_code == 400

    def test_edit_profile(self, sim, client):
        page = client.get("/profiles/web-test/edit")
        assert page.status_code == 200
        assert b"web-test" in page.data
        edited = PROFILE_YAML.replace("apn: hologram", "apn: hologram2")
        response = client.post("/profiles/web-test/edit", data={"yaml_text": edited})
        assert response.status_code == 302
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert profiles[0].pdp_contexts[0].apn == "hologram2"

    def test_edit_rename_moves_profile(self, sim, client):
        renamed = PROFILE_YAML.replace("name: web-test", "name: renamed")
        client.post("/profiles/web-test/edit", data={"yaml_text": renamed})
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert {p.name for p in profiles} == {"renamed"}

    def test_delete_profile(self, sim, client):
        response = client.post("/profiles/web-test/delete")
        assert response.status_code == 302
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert profiles == []

    def test_force_and_release(self, sim, client):
        client.post("/profiles/web-test/force")
        client.post("/profiles/release-force")
        drained = sim.commands.drain()
        assert cmd.ForceProfile("web-test") in drained
        assert cmd.ReleaseForce() in drained


class TestDiagnostics:
    def test_page_renders(self, sim, client):
        page = client.get("/diagnostics/")
        assert page.status_code == 200
        assert b"Run standard diagnostics" in page.data

    def test_run_standard_bundle(self, sim, client):
        response = client.post("/diagnostics/run", data={"commands": ""})
        assert response.status_code == 302
        assert cmd.RunDiagnostics(commands=()) in sim.commands.drain()

    def test_run_custom_commands(self, sim, client):
        client.post("/diagnostics/run", data={"commands": "AT+CEREG?\n\n at+csq \n"})
        assert cmd.RunDiagnostics(commands=("AT+CEREG?", "at+csq")) in sim.commands.drain()

    def test_non_at_commands_rejected(self, sim, client):
        client.post("/diagnostics/run", data={"commands": "rm -rf /"})
        assert sim.commands.drain() == []

    def test_results_render(self, sim, client):
        tick_until_connected(sim)
        sim.commands.put(cmd.RunDiagnostics())
        sim.daemon.tick()
        page = client.get("/diagnostics/")
        assert b"AT+CSQ" in page.data
        assert b"+CSQ: 18,99" in page.data


class TestLogs:
    def test_events_page(self, sim, client):
        tick_until_connected(sim)
        page = client.get("/events")
        assert page.status_code == 200
        assert b"CONNECTED" in page.data  # state transition event rendered

    def test_events_filter(self, sim, client):
        tick_until_connected(sim)
        page = client.get("/events?kind=pdp")
        assert page.status_code == 200
        assert b"PDP" in page.data or b"pdp" in page.data

    def test_monitor_page_empty(self, sim, client):
        page = client.get("/monitor")
        assert page.status_code == 200
        assert b"No monitor results yet" in page.data

    def test_monitor_page_with_results(self, sim, client):
        sim.db.add_monitor_result("https://x.example/hb", 200, 123.4, ok=True)
        page = client.get("/monitor")
        assert b"x.example" in page.data
        assert b"ok" in page.data

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

    def test_favicon_served(self, sim, client):
        resp = client.get("/favicon.svg")
        assert resp.status_code == 200
        assert b"<svg" in resp.data


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
        data = client.get("/api/timeline.json").get_json()
        assert any(r["source"] == "event" for r in data["rows"])
        assert "total" in data and "kinds" in data

    def test_timeline_pagination_and_filter(self, sim, client):
        tick_until_connected(sim)
        for i in range(60):
            sim.db.add_event("info", "test", f"row {i}")
        page = client.get("/api/timeline.json?limit=20&offset=0").get_json()
        assert len(page["rows"]) == 20 and page["total"] >= 60
        page2 = client.get("/api/timeline.json?limit=20&offset=20").get_json()
        assert page2["rows"][0]["ts"] <= page["rows"][-1]["ts"]  # older
        filtered = client.get("/api/timeline.json?source=event&kind=test").get_json()
        assert all(r["kind"] == "test" for r in filtered["rows"])
        assert filtered["total"] == 60

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

    def test_urcs_after_tails_incrementally(self, sim, client):
        tick_until_connected(sim)
        sim.daemon.driver.push_urc("ring", {}, raw="RING")
        sim.daemon.tick()
        rows = client.get("/api/urcs.json").get_json()  # newest-first
        last_id = max(r["id"] for r in rows)
        sim.daemon.driver.push_urc(
            "caller_id", {"number": "+15550001111", "type": 145},
            raw='+CLIP: "+15550001111",145',
        )
        sim.daemon.tick()
        tail = client.get(f"/api/urcs.json?after={last_id}").get_json()
        assert [r["raw"] for r in tail] == ['+CLIP: "+15550001111",145']
        assert all(r["id"] > last_id for r in tail)
        # no new rows -> empty tail
        newest = max(r["id"] for r in tail)
        assert client.get(f"/api/urcs.json?after={newest}").get_json() == []

    def test_debug_urc_injection(self, sim, client):
        tick_until_connected(sim)
        resp = client.post("/api/debug/urc", json={"raw": '+CLIP: "+15550001111",145'})
        assert resp.status_code == 200
        assert resp.get_json()["kind"] == "caller_id"
        sim.daemon.tick()
        rows = client.get("/api/urcs.json").get_json()
        assert rows[0]["kind"] == "caller_id"
        events = client.get("/api/events.json").get_json()
        assert any("incoming call from +15550001111" in e["message"] for e in events)

    def test_debug_urc_rejected_outside_simulate(self, sim, client):
        sim.config.simulate = False
        try:
            resp = client.post("/api/debug/urc", json={"raw": "RING"})
            assert resp.status_code == 403
        finally:
            sim.config.simulate = True

    def test_events_and_monitor_json(self, sim, client):
        tick_until_connected(sim)
        assert client.get("/api/events.json").status_code == 200
        assert client.get("/api/monitor.json").status_code == 200

    def test_monitor_history_paginated(self, sim, client):
        for i in range(30):
            sim.db.add_monitor_result(f"https://x/{i}", 200, 12.0, ok=True)
        page = client.get("/api/monitor.json?limit=10&offset=0").get_json()
        assert page["total"] == 30
        assert len(page["results"]) == 10
        assert page["limit"] == 10
        page2 = client.get("/api/monitor.json?limit=10&offset=10").get_json()
        # offset returns different (older) rows
        assert page2["results"][0]["url"] != page["results"][0]["url"]


class TestLatencyJson:
    def test_empty_window_ok(self, sim, client):
        data = client.get("/api/latency.json").get_json()
        assert data["interfaces"] == []
        assert data["series"] == {}
        assert data["source"] == "raw"

    def test_raw_samples_returned_and_summarized(self, sim, client):
        import time as _t

        now = _t.time()
        sim.db.add_icmp_samples(now - 30, [
            {"interface": "wwan0", "target": "1.1.1.1", "sent": 5, "received": 5,
             "loss_pct": 0.0, "rtt_avg_ms": 40.0, "rtt_min_ms": 30.0, "rtt_max_ms": 50.0},
            {"interface": "eth0", "target": "1.1.1.1", "sent": 5, "received": 4,
             "loss_pct": 20.0, "rtt_avg_ms": 8.0, "rtt_min_ms": 7.0, "rtt_max_ms": 9.0},
        ])
        data = client.get(f"/api/latency.json?from={now - 3600}&to={now}").get_json()
        assert set(data["interfaces"]) == {"wwan0", "eth0"}
        assert "wwan0|1.1.1.1" in data["series"]
        assert data["headline"]["eth0"]["loss_pct"] == 20.0

    def test_long_window_uses_rollups(self, sim, client):
        import time as _t

        now = _t.time()
        # a daily window (>14d) should hit the 'day' rollup source
        data = client.get(f"/api/latency.json?from={now - 30 * 86400}&to={now}").get_json()
        assert data["source"] == "day"

    def test_interface_filter(self, sim, client):
        import time as _t

        now = _t.time()
        sim.db.add_icmp_samples(now - 30, [
            {"interface": "wwan0", "target": "1.1.1.1", "sent": 5, "received": 5,
             "loss_pct": 0.0, "rtt_avg_ms": 40.0, "rtt_min_ms": 30.0, "rtt_max_ms": 50.0},
            {"interface": "eth0", "target": "1.1.1.1", "sent": 5, "received": 5,
             "loss_pct": 0.0, "rtt_avg_ms": 8.0, "rtt_min_ms": 7.0, "rtt_max_ms": 9.0},
        ])
        data = client.get(
            f"/api/latency.json?from={now - 3600}&to={now}&interface=eth0"
        ).get_json()
        assert data["interfaces"] == ["eth0"]

    def test_csv_export(self, sim, client):
        import time as _t

        now = _t.time()
        sim.db.add_icmp_samples(now - 30, [
            {"interface": "wwan0", "target": "1.1.1.1", "sent": 5, "received": 4,
             "loss_pct": 20.0, "rtt_avg_ms": 40.0, "rtt_min_ms": 30.0, "rtt_max_ms": 50.0},
        ])
        resp = client.get(f"/api/latency.csv?from={now - 3600}&to={now}")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert "attachment" in resp.headers["Content-Disposition"]
        body = resp.get_data(as_text=True)
        lines = body.strip().splitlines()
        assert lines[0].startswith("ts_iso,ts_epoch,source,interface,target")
        assert "wwan0,1.1.1.1,5,4,20.0,40.0,30.0,50.0" in body


class TestLatencyConfig:
    def test_get_returns_config_default(self, sim, client):
        data = client.get("/api/latency-config.json").get_json()
        assert "enabled" in data and "targets" in data
        assert data["enabled"] is False  # default off

    def test_put_persists_and_hot_reloads(self, sim, client):
        from sim_monitor.monitor.ping_monitor import effective_latency_config

        body = {"enabled": True, "interval_seconds": 90, "packet_count": 3,
                "targets": ["1.1.1.1", "8.8.8.8"]}
        resp = client.put("/api/latency-config", json=body)
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
        # stored + reflected back through the GET endpoint
        got = client.get("/api/latency-config.json").get_json()
        assert got["enabled"] is True and got["interval_seconds"] == 90
        # the resolver the ping thread uses now sees the new config (hot-reload)
        eff = effective_latency_config(sim.db, sim.config.latency)
        assert eff.enabled is True and eff.interval_seconds == 90

    def test_put_invalid_400(self, sim, client):
        resp = client.put("/api/latency-config", json={"interval_seconds": 1})
        assert resp.status_code == 400

    def test_interface_colors_roundtrip(self, sim, client):
        body = {"enabled": True, "interface_colors": {"wlan0": "#3b82f6", "wwan0": "#f59e0b"}}
        assert client.put("/api/latency-config", json=body).status_code == 200
        got = client.get("/api/latency-config.json").get_json()
        assert got["interface_colors"]["wlan0"] == "#3b82f6"

    def test_interface_colors_bad_hex_400(self, sim, client):
        resp = client.put("/api/latency-config", json={"interface_colors": {"wlan0": "blue"}})
        assert resp.status_code == 400


class TestHttpChecksConfig:
    def test_get_returns_config_default(self, sim, client):
        data = client.get("/api/http-checks-config.json").get_json()
        assert data["enabled"] is False
        assert any(t.startswith("http") for t in data["targets"])

    def test_put_persists_and_hot_reloads(self, sim, client):
        from sim_monitor.monitor.http_check_monitor import effective_http_check_config

        body = {"enabled": True, "interval_seconds": 90,
                "targets": ["https://google.com/generate_204"], "timeout_seconds": 8}
        resp = client.put("/api/http-checks-config", json=body)
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
        got = client.get("/api/http-checks-config.json").get_json()
        assert got["enabled"] is True and got["interval_seconds"] == 90
        eff = effective_http_check_config(sim.db, sim.config.http_checks)
        assert eff.enabled is True and eff.timeout_seconds == 8

    def test_put_rejects_non_url_target(self, sim, client):
        resp = client.put("/api/http-checks-config", json={"targets": ["1.1.1.1"]})
        assert resp.status_code == 400

    def test_data_endpoint_returns_status_in_series(self, sim, client):
        sim.db.add_http_samples(1_000_000.0, [
            {"interface": "wwan0", "target": "https://x", "ok": 1,
             "status_code": 204, "latency_ms": 120},
        ])
        data = client.get("/api/http-checks.json?from=999000&to=1000001").get_json()
        assert data["source"] == "raw"
        key = "wwan0|https://x"
        assert key in data["series"]
        assert data["series"][key][0]["status_code"] == 204

    def test_csv_export_has_url_and_status_columns(self, sim, client):
        sim.db.add_http_samples(1_000_000.0, [
            {"interface": "wwan0", "target": "https://x", "ok": 0,
             "status_code": 503, "latency_ms": None},
        ])
        body = client.get("/api/http-checks.csv?from=999000&to=1000001").get_data(as_text=True)
        lines = body.splitlines()
        assert lines[0].startswith("ts_iso,ts_epoch,source,interface,url,status_code")
        assert "wwan0,https://x,503" in body


class TestSmsAutoReplyConfig:
    def test_get_returns_disabled_default(self, sim, client):
        data = client.get("/api/sms-autoreply.json").get_json()
        assert data["enabled"] is False and data["rules"] == []

    def test_put_persists_and_roundtrips(self, sim, client):
        body = {"enabled": True, "rules": [
            {"name": "status", "match": "contains", "pattern": "status", "reply": "ok"},
        ]}
        resp = client.put("/api/sms-autoreply", json=body)
        assert resp.status_code == 200 and resp.get_json()["ok"] is True
        got = client.get("/api/sms-autoreply.json").get_json()
        assert got["enabled"] is True
        assert got["rules"][0]["pattern"] == "status" and got["rules"][0]["reply"] == "ok"

    def test_put_invalid_regex_400(self, sim, client):
        body = {"enabled": True, "rules": [
            {"match": "regex", "pattern": "[bad", "reply": "x"},
        ]}
        assert client.put("/api/sms-autoreply", json=body).status_code == 400

    def test_put_blank_reply_400(self, sim, client):
        body = {"enabled": True, "rules": [{"pattern": "hi", "reply": ""}]}
        assert client.put("/api/sms-autoreply", json=body).status_code == 400


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
        assert any(m["body"] == "field test message" for m in data["results"])


class TestScanApi:
    def _wait_done(self, client, timeout=3.0):
        import time
        start = time.time()
        while time.time() - start < timeout:
            s = client.get("/api/scan.json").get_json()
            if not s["running"]:
                return s
            time.sleep(0.02)
        raise AssertionError("scan did not finish")

    def test_interfaces(self, sim, client):
        data = client.get("/api/scan/interfaces.json").get_json()
        assert any(i["name"] == "wwan0" for i in data)

    def test_discovery_flow(self, sim, client):
        resp = client.post("/api/scan/discovery", json={"cidr": "192.168.1.0/24"})
        assert resp.status_code == 200
        s = self._wait_done(client)
        assert s["kind"] == "discovery" and s["results"]

    def test_reachability_flow(self, sim, client):
        client.post("/api/scan/reachability", json={"target": "example.com", "interface": "wwan0"})
        s = self._wait_done(client)
        assert s["summary"]["http"]["status"] == 200

    def test_traceroute_flow(self, sim, client):
        client.post("/api/scan/traceroute", json={"target": "example.com"})
        s = self._wait_done(client)
        assert s["summary"]["reached"] is True

    def test_bad_cidr_400(self, sim, client):
        assert client.post("/api/scan/discovery", json={"cidr": "nope"}).status_code == 400

    def test_missing_target_400(self, sim, client):
        assert client.post("/api/scan/reachability", json={}).status_code == 400

    def test_stop(self, sim, client):
        assert client.post("/api/scan/stop").status_code == 200


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

    def test_update_command_removed(self, sim, client):
        # The in-UI device-update feature was removed; updates are git+install.
        assert client.post("/api/cmd/update-app").status_code == 404


class TestMonitorConfigApi:
    def test_default_disabled(self, sim, client):
        data = client.get("/api/monitor-config.json").get_json()
        assert data["enabled"] is False

    def test_set_global_config(self, sim, client):
        cfg = {
            "enabled": True,
            "destinations": [
                {"egress": "wlan", "method": "POST",
                 "url": "https://hooks.example.com/hb", "interval_seconds": 120},
            ],
        }
        assert client.put("/api/monitor-config", json=cfg).status_code == 200
        sim.daemon.tick()  # process ReloadMonitorConfig
        eff = sim.daemon.effective_monitor_config()
        assert eff is not None and eff.enabled
        assert eff.destinations[0].interval_seconds == 120
        got = client.get("/api/monitor-config.json").get_json()
        assert got["destinations"][0]["interval_seconds"] == 120

    def test_invalid_config_400(self, sim, client):
        # A destination without a URL is invalid.
        resp = client.put("/api/monitor-config", json={"destinations": [{"egress": "wlan"}]})
        assert resp.status_code == 400

    def test_body_fields_roundtrip(self, sim, client):
        cfg = {
            "enabled": True,
            "body_fields": [
                {"path": "iccid", "value": "iccid", "kind": "placeholder"},
                {"path": "signal.rsrp_dbm", "value": "rsrp", "kind": "placeholder"},
            ],
            "destinations": [{"egress": "wlan", "url": "https://hooks.example.com/ingest"}],
        }
        assert client.put("/api/monitor-config", json=cfg).status_code == 200
        got = client.get("/api/monitor-config.json").get_json()
        assert got["body_fields"][1]["path"] == "signal.rsrp_dbm"

    def test_placeholders_endpoint(self, sim, client):
        tick_until_connected(sim)
        ctx = client.get("/api/placeholders.json").get_json()
        assert ctx["iccid"] == "8944500612345678901"
        assert "rsrp" in ctx and "status" in ctx and "sampled_at" in ctx
        # latency placeholders always present (None until data exists)
        assert "latency_ms" in ctx and "loss_pct" in ctx
        assert "latency_24h" in ctx and "loss_1h" in ctx

    def test_placeholders_include_latency_values(self, sim, client):
        import time as _t

        tick_until_connected(sim)
        iface = sim.store.get().interface
        sim.db.add_icmp_samples(_t.time() - 20, [
            {"interface": iface, "target": "1.1.1.1", "sent": 5, "received": 5,
             "loss_pct": 0.0, "rtt_avg_ms": 42.0, "rtt_min_ms": 40.0, "rtt_max_ms": 44.0},
        ])
        ctx = client.get("/api/placeholders.json").get_json()
        assert ctx["latency_ms"] == 42.0 and ctx["loss_pct"] == 0.0
        assert ctx["latency_24h"] == 42.0

    def test_profile_override_wins_when_enabled(self, sim, client):
        # Global disabled; an enabled profile monitor overrides it.
        prof_yaml = (
            "name: ov\nmatch: {iccid_patterns: ['*'], priority: 5}\n"
            "pdp_contexts: [{cid: 1, apn: hologram, bearer: true}]\n"
            "monitor:\n  enabled: true\n  destinations:\n"
            "    - {egress: cellular, method: POST, url: 'https://p/override',\n"
            "       interval_seconds: 45}\n"
        )
        client.post("/api/profiles", json={"yaml": prof_yaml})
        tick_until_connected(sim)  # active profile becomes 'ov' (priority 5 beats 1000)
        eff = sim.daemon.effective_monitor_config()
        assert eff is not None and eff.destinations[0].url == "https://p/override"


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

    def test_export_then_import_roundtrip(self, sim, client):
        # Add a second profile, export the set.
        extra = (
            "name: extra\nmatch: {iccid_patterns: ['8946*'], priority: 30}\n"
            "pdp_contexts: [{cid: 1, apn: vzwinternet, bearer: true}]\n"
        )
        client.post("/api/profiles", json={"yaml": extra})
        bundle = client.get("/api/profiles/export.json")
        assert bundle.status_code == 200
        assert "attachment" in bundle.headers["Content-Disposition"]
        data = bundle.get_json()
        assert data["schema"] == "sim-monitor/profiles@1"
        names = {p["name"] for p in data["profiles"]}
        assert {"web-test", "extra"} <= names

        # Import the bundle into a fresh device.
        from sim_monitor.config.loader import save_profile  # noqa: F401  (clarity)
        for f in (sim.config.profiles_dir).glob("*.yaml"):
            f.unlink()
        result = client.post("/api/profiles/import", json=data).get_json()
        assert result["imported"] == len(data["profiles"])
        assert result["errors"] == []
        profiles, _ = load_profiles(sim.config.profiles_dir)
        assert {"web-test", "extra"} <= {p.name for p in profiles}

    def test_import_reports_bad_profiles(self, sim, client):
        result = client.post("/api/profiles/import", json={
            "profiles": [
                {"name": "good", "match": {"iccid_patterns": ["*"], "priority": 1},
                 "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}]},
                {"name": "bad", "pdp_contexts": []},  # invalid
            ]
        }).get_json()
        assert result["imported"] == 1
        assert len(result["errors"]) == 1 and result["errors"][0]["name"] == "bad"

    def test_import_accepts_bare_list(self, sim, client):
        result = client.post("/api/profiles/import", json=[
            {"name": "fromlist", "match": {"iccid_patterns": ["*"], "priority": 9},
             "pdp_contexts": [{"cid": 1, "apn": "hologram", "bearer": True}]},
        ]).get_json()
        assert result["imported"] == 1


class TestTrafficApi:
    def _flow(self, **kw):
        base = {
            "first_seen": 1000.0, "last_seen": 1010.0, "proto": "tcp",
            "direction": "out", "remote_ip": "1.2.3.4", "remote_port": 443,
            "local_ip": "10.0.0.5", "local_port": 40000, "interface": "wwan0",
            "bytes_sent": 100, "bytes_recv": 200,
            "packets_sent": 2, "packets_recv": 3, "active": 0,
        }
        base.update(kw)
        return base

    def test_flows_filtering(self, sim, client):
        sim.db.add_traffic_flow(self._flow())
        sim.db.add_traffic_flow(self._flow(remote_ip="5.6.7.8", proto="udp",
                                           remote_port=53, interface="wlan0"))
        data = client.get("/api/traffic/flows.json").get_json()
        assert data["total"] == 2
        data = client.get("/api/traffic/flows.json?ip=1.2.3.4").get_json()
        assert data["total"] == 1
        assert data["flows"][0]["remote_port"] == 443
        data = client.get("/api/traffic/flows.json?port=53&proto=udp").get_json()
        assert data["total"] == 1
        data = client.get("/api/traffic/flows.json?interface=wlan0").get_json()
        assert data["total"] == 1 and data["flows"][0]["interface"] == "wlan0"
        data = client.get("/api/traffic/flows.json?ip=!5.6.7.8").get_json()
        assert data["total"] == 1 and data["flows"][0]["remote_ip"] == "1.2.3.4"
        data = client.get(
            "/api/traffic/flows.json?sort=remote_port&order=asc"
        ).get_json()
        assert [f["remote_port"] for f in data["flows"]] == [53, 443]

    def test_summary_respects_filters(self, sim, client):
        sim.db.add_traffic_flow(self._flow(bytes_sent=1000))
        sim.db.add_traffic_flow(self._flow(remote_ip="5.6.7.8", interface="wlan0",
                                           bytes_sent=7))
        data = client.get("/api/traffic/summary.json?interface=wwan0").get_json()
        assert data["totals"]["out"]["bytes_sent"] == 1000
        assert [r["remote_ip"] for r in data["top_remotes"]] == ["1.2.3.4"]

    def test_summary(self, sim, client):
        sim.db.add_traffic_flow(self._flow(bytes_sent=1000))
        data = client.get("/api/traffic/summary.json").get_json()
        assert data["totals"]["out"]["bytes_sent"] == 1000
        assert data["top_remotes"][0]["remote_ip"] == "1.2.3.4"
        assert data["by_interface"][0]["interface"] == "wwan0"
        assert "status" in data

    def test_config_roundtrip(self, sim, client):
        cfg = client.get("/api/traffic-config.json").get_json()
        assert cfg["enabled"] is True  # default: audit on
        cfg["retention_days"] = 9
        assert client.put("/api/traffic-config", json=cfg).status_code == 200
        assert client.get("/api/traffic-config.json").get_json()["retention_days"] == 9

    def test_config_rejects_invalid(self, sim, client):
        resp = client.put("/api/traffic-config", json={"retention_days": 0})
        assert resp.status_code == 400

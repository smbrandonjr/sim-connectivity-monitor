from sim_monitor.core.diagnostics import build_bundle, build_timeline

EVENTS = [
    {"ts": 100.0, "kind": "state", "message": "CONNECTING -> CONNECTED"},
    {"ts": 90.0, "kind": "ota", "message": "SIM identity change"},
]
URCS = [
    {"ts": 95.0, "kind": "sim_status", "raw": "+QSIMSTAT: 1,1"},
    {"ts": 80.0, "kind": "new_sms", "raw": '+CMTI: "ME",1'},
]
IDENTITY = [
    {"ts": 92.0, "iccid": "8946420000000000999", "imsi": "310030000000001",
     "registration": "registered-roaming", "reason": "ota-swap"},
]


class TestBuildTimeline:
    def test_merged_and_sorted_desc(self):
        rows = build_timeline(EVENTS, URCS, IDENTITY)
        assert [r["ts"] for r in rows] == [100.0, 95.0, 92.0, 90.0, 80.0]
        assert {r["source"] for r in rows} == {"event", "urc", "identity"}

    def test_identity_detail_includes_iccid(self):
        rows = build_timeline([], [], IDENTITY)
        assert "8946420000000000999" in rows[0]["detail"]

    def test_limit(self):
        assert len(build_timeline(EVENTS, URCS, IDENTITY, limit=2)) == 2


class TestBuildBundle:
    def _snapshot(self):
        return {
            "vendor": "Quectel", "model": "EC25", "firmware": "EC25AFXGAR07A04M1G",
            "imei": "868105049461864", "iccid": "894642...", "imsi": "310030...",
            "operator": "T-Mobile", "registration": "registered-roaming",
            "state": "CONNECTED", "interface": "wwan0", "ip_address": "10.0.0.2",
            "signal_rssi": -67, "signal_percent": 74, "last_error": None,
        }

    def test_shape_and_modem_section(self):
        bundle = build_bundle(
            generated_at=123.0, app_version="0.1.0", snapshot=self._snapshot(),
            active_profile=None, events=EVENTS, urcs=URCS, identity=IDENTITY,
        )
        assert bundle["schema"] == "sim-monitor/diagnostic-bundle@1"
        assert bundle["modem"]["firmware"] == "EC25AFXGAR07A04M1G"
        assert bundle["sim"]["operator"] == "T-Mobile"
        assert bundle["events"] == EVENTS

    def test_strips_secrets_from_profile(self):
        profile = {
            "name": "vzw",
            "pdp_contexts": [
                {"cid": 1, "apn": "vzwinternet", "password": "s3cret", "bearer": True}
            ],
            "monitor": {
                "enabled": True,
                "body": '{"x":1}',
                "destinations": [
                    {
                        "egress": "cellular", "url": "https://x/y?token=abc",
                        "headers": {"Authorization": "Bearer T"}, "interval_seconds": 300,
                    },
                ],
            },
        }
        bundle = build_bundle(
            generated_at=1.0, app_version="0.1.0", snapshot=self._snapshot(),
            active_profile=profile, events=[], urcs=[], identity=[],
        )
        ap = bundle["active_profile"]
        assert ap["pdp_contexts"][0]["password"] == "***"
        # monitor URL/headers/body dropped; only shape (egress + interval) kept
        assert ap["monitor"] == {
            "enabled": True,
            "destinations": [{"egress": "cellular", "interval_seconds": 300}],
        }

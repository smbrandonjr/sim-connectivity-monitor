import json

from sim_monitor.monitor.placeholders import render, render_body_fields, render_request


class TestRender:
    def test_simple_substitution(self):
        result, unknown = render("iccid={iccid}", {"iccid": "8944500612345678901"})
        assert result == "iccid=8944500612345678901"
        assert unknown == set()

    def test_json_body_braces_untouched(self):
        template = '{"iccid":"{iccid}","rssi":{signal_rssi},"nested":{"a":1}}'
        result, unknown = render(template, {"iccid": "89445", "signal_rssi": -77})
        assert result == '{"iccid":"89445","rssi":-77,"nested":{"a":1}}'
        assert unknown == set()

    def test_unknown_token_left_intact_and_reported(self):
        result, unknown = render("x={nope}", {"iccid": "1"})
        assert result == "x={nope}"
        assert unknown == {"nope"}

    def test_none_value_renders_empty(self):
        result, _ = render("op={operator}", {"operator": None})
        assert result == "op="

    def test_uppercase_not_a_token(self):
        result, unknown = render("{ICCID} {Iccid}", {"iccid": "1"})
        assert result == "{ICCID} {Iccid}"
        assert unknown == set()

    def test_numbers_and_underscores_in_names(self):
        result, _ = render("{signal_rssi}", {"signal_rssi": -90})
        assert result == "-90"


class TestRenderRequest:
    def test_all_parts_rendered(self):
        url, headers, body, unknown = render_request(
            "https://api.example.com/device/{iccid}",
            {"Authorization": "Bearer tok", "X-IMEI": "{imei}"},
            '{"ts":"{timestamp}"}',
            {"iccid": "894", "imei": "490", "timestamp": "2026-01-01T00:00:00Z"},
        )
        assert url == "https://api.example.com/device/894"
        assert headers == {"Authorization": "Bearer tok", "X-IMEI": "490"}
        assert body == '{"ts":"2026-01-01T00:00:00Z"}'
        assert unknown == set()

    def test_unknowns_aggregated(self):
        _, _, _, unknown = render_request("{a}", {"h": "{b}"}, "{c}", {})
        assert unknown == {"a", "b", "c"}


class TestRenderBodyFields:
    def _f(self, path, value, kind="placeholder"):
        return {"path": path, "value": value, "kind": kind}

    def test_nested_and_typed(self):
        fields = [
            self._f("iccid", "iccid"),
            self._f("status", "status"),
            self._f("signal.rsrp_dbm", "rsrp"),
            self._f("signal.band", "band"),
            self._f("meta.fw", "firmware"),
        ]
        ctx = {"iccid": "894", "status": "connected", "rsrp": -95, "band": 2, "firmware": "EC25"}
        out = json.loads(render_body_fields(fields, ctx))
        assert out == {
            "iccid": "894", "status": "connected",
            "signal": {"rsrp_dbm": -95, "band": 2},  # numbers stay numbers
            "meta": {"fw": "EC25"},
        }

    def test_unknown_values_omitted_keeps_valid_json(self):
        fields = [self._f("signal.rsrp_dbm", "rsrp"), self._f("signal.sinr_db", "sinr")]
        ctx = {"rsrp": -95, "sinr": None}  # sinr unknown
        out = json.loads(render_body_fields(fields, ctx))
        assert out == {"signal": {"rsrp_dbm": -95}}  # sinr dropped, still valid

    def test_static_field(self):
        fields = [self._f("meta.tag", "warehouse", kind="static")]
        out = json.loads(render_body_fields(fields, {}))
        assert out == {"meta": {"tag": "warehouse"}}

    def test_empty_fields_is_empty_object(self):
        assert render_body_fields([], {}) == "{}"

    def test_deep_nesting_coexists_under_meta(self):
        # The latency placeholders land at meta.latency.* and must merge with
        # other meta.* fields rather than clobbering the meta object.
        fields = [
            self._f("meta.imei", "imei"),
            self._f("meta.latency.last_ms", "latency_ms"),
            self._f("meta.latency.avg_ms_1h", "latency_1h"),
            self._f("meta.latency.loss_pct_1h", "loss_1h"),
        ]
        ctx = {"imei": "490154", "latency_ms": 42.6, "latency_1h": 47.2, "loss_1h": 1.4}
        out = json.loads(render_body_fields(fields, ctx))
        assert out == {
            "meta": {
                "imei": "490154",
                "latency": {"last_ms": 42.6, "avg_ms_1h": 47.2, "loss_pct_1h": 1.4},
            },
        }

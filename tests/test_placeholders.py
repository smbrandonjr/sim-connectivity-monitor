from sim_monitor.monitor.placeholders import render, render_request


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

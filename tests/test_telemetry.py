from sim_monitor.modem.at_parser import (
    parse_qcsq,
    parse_qeng_servingcell,
    parse_qnwinfo,
)
from sim_monitor.web.routes.telemetry import sparkline_points


class TestQcsq:
    def test_lte(self):
        r = parse_qcsq(['+QCSQ: "LTE",-66,-94,142,-9'])
        assert r == {"rat": "LTE", "rssi": -66, "rsrp": -94, "sinr": 142, "rsrq": -9}

    def test_no_service(self):
        r = parse_qcsq(['+QCSQ: "NOSERVICE"'])
        assert r["rat"] == "NOSERVICE" and r["rsrp"] is None

    def test_missing(self):
        assert parse_qcsq(["OK"]) is None


class TestQengServingCell:
    def test_lte_full(self):
        line = (
            '+QENG: "servingcell","NOCONN","LTE","FDD",310,260,1A2D04,'
            "176,5110,13,5,5,1A2B,-94,-9,-66,14,21"
        )
        r = parse_qeng_servingcell([line])
        assert r["rat"] == "LTE"
        assert r["mcc"] == 310 and r["mnc"] == 260
        assert r["cell_id"] == "1A2D04"
        assert r["pci"] == 176
        assert r["band"] == 13
        assert r["tac"] == "1A2B"
        assert r["rsrp"] == -94 and r["rsrq"] == -9 and r["sinr"] == 14

    def test_non_lte_graceful(self):
        r = parse_qeng_servingcell(['+QENG: "servingcell","SEARCH"'])
        assert r["state"] == "SEARCH"


class TestQnwinfo:
    def test_lte(self):
        r = parse_qnwinfo(['+QNWINFO: "FDD LTE","311480","LTE BAND 13",2150'])
        assert r["operator_numeric"] == "311480"
        assert r["band"] == "LTE BAND 13"
        assert r["channel"] == 2150

    def test_no_service(self):
        r = parse_qnwinfo(["+QNWINFO: No Service"])
        assert r["operator_numeric"] is None


class TestSparkline:
    def test_points_generated(self):
        pts = sparkline_points([-100, -90, -80], width=200, height=40)
        assert pts.count(",") == 3  # three points
        assert pts.startswith("0.0,")

    def test_too_few_points(self):
        assert sparkline_points([]) == ""
        assert sparkline_points([-90]) == ""

    def test_flat_series_no_div_by_zero(self):
        pts = sparkline_points([-90, -90, -90])
        assert pts  # does not raise


class TestQuectelTelemetry:
    def test_assembles_from_three_queries(self):
        from sim_monitor.modem.drivers import QuectelDriver
        from tests.test_drivers import ScriptedChannel

        channel = ScriptedChannel({
            "AT+QCSQ": ['+QCSQ: "LTE",-66,-94,142,-9'],
            'AT+QENG="servingcell"': [
                '+QENG: "servingcell","NOCONN","LTE","FDD",310,260,1A2D04,'
                "176,5110,13,5,5,1A2B,-94,-9,-66,14,21"
            ],
            "AT+QNWINFO": ['+QNWINFO: "FDD LTE","311480","LTE BAND 13",2150'],
        })
        t = QuectelDriver(channel).get_telemetry()
        assert t["rsrp"] == -94 and t["band"] == 13
        assert t["operator_numeric"] == "311480"
        assert t["cell_id"] == "1A2D04"

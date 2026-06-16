"""Connectivity-uptime analytics (pure) + the connectivity storage round-trip."""

from sim_monitor.core.uptime import summarize
from sim_monitor.storage.db import Database


class TestSummarize:
    def test_all_connected(self):
        s = summarize(True, [], 0, 100)
        assert s["connected_s"] == 100 and s["down_s"] == 0
        assert s["uptime_pct"] == 100 and s["outage_count"] == 0

    def test_all_down_is_one_ongoing_outage(self):
        s = summarize(False, [], 0, 100)
        assert s["down_s"] == 100 and s["uptime_pct"] == 0
        assert s["outage_count"] == 1
        assert s["episodes"][0]["ongoing"] is True
        assert s["episodes"][0]["duration_s"] == 100

    def test_mixed_window_with_closed_outage(self):
        # up until 30, down 30-50 ("lost"), up again to 100
        s = summarize(True, [(30, False, "lost"), (50, True, None)], 0, 100)
        assert s["connected_s"] == 80 and s["down_s"] == 20
        assert s["uptime_pct"] == 80
        assert s["outage_count"] == 1
        ep = s["episodes"][0]
        assert (ep["start"], ep["end"], ep["duration_s"], ep["detail"]) == (30, 50, 20, "lost")
        assert ep["ongoing"] is False

    def test_ongoing_outage_at_window_end(self):
        s = summarize(True, [(60, False, "connection lost")], 0, 100)
        assert s["down_s"] == 40 and s["currently_up"] is False
        assert s["episodes"][-1]["ongoing"] is True
        assert s["longest_outage_s"] == 40

    def test_edges_clamped_to_window(self):
        # an edge before the window start is clamped to t0
        s = summarize(False, [(-50, True, None)], 0, 100)
        assert s["connected_s"] == 100 and s["down_s"] == 0

    def test_zero_width_window(self):
        s = summarize(True, [], 50, 50)
        assert s["uptime_pct"] is None  # no elapsed time


def test_connectivity_storage_round_trip():
    db = Database(":memory:")
    db.add_connectivity(False, "NO_MODEM", "monitor started")
    db.add_connectivity(True, "CONNECTED", None)
    db.add_connectivity(False, "DEGRADED", "connection lost")

    assert db.connectivity_last()["up"] == 0
    assert db.connectivity_last()["detail"] == "connection lost"

    rows = db.connectivity_between(0, 2e10)
    assert [r["up"] for r in rows] == [0, 1, 0]

    at = db.connectivity_state_at(2e10)
    assert at["state"] == "DEGRADED"
    db.close()

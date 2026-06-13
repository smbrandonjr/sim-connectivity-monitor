from __future__ import annotations

from flask import Blueprint, render_template

from sim_monitor.web.routes._helpers import sim

bp = Blueprint("telemetry", __name__)

# Metrics we chart, with friendly labels and units.
SERIES = [
    ("rsrp", "RSRP", "dBm"),
    ("rsrq", "RSRQ", "dB"),
    ("sinr", "SINR", "dB"),
    ("rssi", "RSSI", "dBm"),
]


def sparkline_points(values: list[float], width: int = 280, height: int = 40) -> str:
    """Build an SVG polyline 'points' string from a numeric series (no deps)."""
    nums = [v for v in values if v is not None]
    if len(nums) < 2:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1
    step = width / (len(values) - 1)
    pts = []
    for i, v in enumerate(values):
        if v is None:
            continue
        x = i * step
        y = height - (v - lo) / span * (height - 4) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


@bp.get("/telemetry")
def page():
    app = sim()
    rows = list(reversed(app.db.recent_telemetry(limit=300)))  # oldest -> newest
    charts = []
    for key, label, unit in SERIES:
        series = [r.get(key) for r in rows]
        latest = next((v for v in reversed(series) if v is not None), None)
        charts.append({
            "key": key, "label": label, "unit": unit,
            "points": sparkline_points(series), "latest": latest,
        })
    return render_template(
        "telemetry.html", charts=charts, snap=app.store.get(), samples=len(rows)
    )

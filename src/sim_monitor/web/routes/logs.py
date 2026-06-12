from __future__ import annotations

from flask import Blueprint, render_template, request

from sim_monitor.web.routes._helpers import sim

bp = Blueprint("logs", __name__)

EVENT_KINDS = [
    "state", "modem", "sim", "profile", "pdp", "connection",
    "routing", "recovery", "fallback", "monitor", "command",
]


@bp.get("/events")
def events():
    kind = request.args.get("kind") or None
    rows = sim().db.recent_events(limit=300, kind=kind)
    return render_template("events.html", events=rows, kinds=EVENT_KINDS, selected=kind)


@bp.get("/monitor")
def monitor():
    rows = sim().db.recent_monitor_results(limit=200)
    return render_template("monitor.html", results=rows)

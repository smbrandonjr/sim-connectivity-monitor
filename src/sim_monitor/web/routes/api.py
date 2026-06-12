from __future__ import annotations

from dataclasses import asdict

from flask import Blueprint, jsonify

from sim_monitor.web.routes._helpers import sim

bp = Blueprint("api", __name__, url_prefix="/api")


@bp.get("/status.json")
def status():
    app = sim()
    snapshot = app.store.get()
    data = asdict(snapshot)
    data["state"] = snapshot.state.value
    last = app.db.recent_monitor_results(limit=1)
    data["last_monitor"] = last[0] if last else None
    return jsonify(data)

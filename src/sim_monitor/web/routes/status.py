from __future__ import annotations

from flask import Blueprint, render_template

from sim_monitor.web.routes._helpers import sim

bp = Blueprint("status", __name__)


@bp.get("/")
def dashboard():
    app = sim()
    snapshot = app.store.get()
    last_results = app.db.recent_monitor_results(limit=1)
    active = app.daemon.active_profile
    return render_template(
        "dashboard.html",
        snap=snapshot,
        last_monitor=last_results[0] if last_results else None,
        monitor_cfg=active.monitor if active else None,
    )

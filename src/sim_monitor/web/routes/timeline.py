from __future__ import annotations

from flask import Blueprint, render_template

from sim_monitor.core.diagnostics import build_timeline
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("timeline", __name__)


@bp.get("/timeline")
def page():
    app = sim()
    rows = build_timeline(
        events=app.db.recent_events(limit=300),
        urcs=app.db.recent_urcs(limit=300),
        identity=app.db.recent_identity(limit=100),
    )
    return render_template("timeline.html", rows=rows)

from __future__ import annotations

import time
from dataclasses import asdict

from flask import Blueprint, Response, jsonify

from sim_monitor import __version__
from sim_monitor.core.diagnostics import build_bundle, build_timeline
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("api", __name__, url_prefix="/api")


def _snapshot_dict(app) -> dict:
    snapshot = app.store.get()
    data = asdict(snapshot)
    data["state"] = snapshot.state.value
    data["sms_pending"] = app.daemon.sms_pending
    return data


@bp.get("/status.json")
def status():
    app = sim()
    data = _snapshot_dict(app)
    last = app.db.recent_monitor_results(limit=1)
    data["last_monitor"] = last[0] if last else None
    return jsonify(data)


@bp.get("/urcs.json")
def urcs():
    return jsonify(sim().db.recent_urcs(limit=300))


@bp.get("/identity.json")
def identity():
    return jsonify(sim().db.recent_identity(limit=100))


@bp.get("/timeline.json")
def timeline():
    app = sim()
    data = build_timeline(
        events=app.db.recent_events(limit=300),
        urcs=app.db.recent_urcs(limit=300),
        identity=app.db.recent_identity(limit=100),
    )
    return jsonify(data)


def _bundle(app) -> dict:
    profile = app.daemon.active_profile
    return build_bundle(
        generated_at=time.time(),
        app_version=__version__,
        snapshot=_snapshot_dict(app),
        active_profile=profile.model_dump(mode="json") if profile else None,
        events=app.db.recent_events(limit=1000),
        urcs=app.db.recent_urcs(limit=1000),
        identity=app.db.recent_identity(limit=200),
    )


@bp.get("/bundle.json")
def bundle():
    """Downloadable, secret-free diagnostic bundle for sharing/comparison."""
    import json

    app = sim()
    snapshot = app.store.get()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = f"sim-monitor-bundle-{snapshot.iccid or 'nosim'}-{stamp}.json"
    payload = json.dumps(_bundle(app), indent=2)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )

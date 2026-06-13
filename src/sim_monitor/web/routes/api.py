from __future__ import annotations

import time
from dataclasses import asdict

import yaml
from flask import Blueprint, Response, jsonify, request
from pydantic import ValidationError

from sim_monitor import __version__
from sim_monitor.config import loader
from sim_monitor.config.schema import MonitorConfig, Profile
from sim_monitor.core import commands as cmd
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


@bp.get("/sms.json")
def sms():
    return jsonify(sim().db.recent_sms(limit=200))


@bp.get("/telemetry.json")
def telemetry():
    app = sim()
    return jsonify({
        "latest": app.store.get().telemetry,
        "history": list(reversed(app.db.recent_telemetry(limit=500))),
    })


@bp.get("/identity.json")
def identity():
    return jsonify(sim().db.recent_identity(limit=100))


@bp.get("/events.json")
def events():
    kind = request.args.get("kind") or None
    return jsonify(sim().db.recent_events(limit=300, kind=kind))


@bp.get("/monitor.json")
def monitor_results():
    db = sim().db
    limit = max(1, min(request.args.get("limit", 25, type=int), 200))
    offset = max(0, request.args.get("offset", 0, type=int))
    return jsonify({
        "results": db.recent_monitor_results(limit=limit, offset=offset),
        "total": db.count_monitor_results(),
        "limit": limit,
        "offset": offset,
    })


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


# ── command API (JSON; the SPA posts here) ──────────────────────────────────


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.post("/cmd/<name>")
def command(name: str):
    """Enqueue a daemon command by name. Body is JSON for parameterized ones."""
    app = sim()
    body = _body()
    try:
        match name:
            case "reconnect":
                command_obj = cmd.Reconnect()
            case "reset-modem":
                command_obj = cmd.ResetModem()
            case "monitor-now":
                command_obj = cmd.RunMonitorNow()
            case "monitor-pause":
                command_obj = cmd.PauseMonitor()
            case "monitor-resume":
                command_obj = cmd.ResumeMonitor()
            case "fallback-test":
                dur = body.get("duration_seconds")
                command_obj = cmd.StartFallbackTest(
                    duration_seconds=int(dur) if dur else None
                )
            case "fallback-abort":
                command_obj = cmd.AbortFallbackTest()
            case "force-profile":
                command_obj = cmd.ForceProfile(name=str(body["name"]))
            case "release-force":
                command_obj = cmd.ReleaseForce()
            case "reload-profiles":
                command_obj = cmd.ReloadProfiles()
            case "run-diagnostics":
                cmds = tuple(str(c) for c in body.get("commands", []))
                if any(not c.upper().startswith("AT") for c in cmds):
                    return jsonify({"error": "all commands must start with AT"}), 400
                command_obj = cmd.RunDiagnostics(commands=cmds)
            case "send-sms":
                command_obj = cmd.SendSms(number=str(body["number"]), text=str(body["text"]))
            case "delete-sms":
                command_obj = cmd.DeleteSms(row_id=int(body["row_id"]))
            case "clear-sms":
                command_obj = cmd.ClearSms()
            case "refresh-sms":
                command_obj = cmd.RefreshSms()
            case "mark-sms-read":
                command_obj = cmd.MarkSmsRead()
            case "set-sim-name":
                command_obj = cmd.SetSimName(name=str(body.get("name", "")))
            case "update-app":
                return _trigger_update(app)
            case _:
                return jsonify({"error": f"unknown command {name!r}"}), 404
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"invalid arguments: {e}"}), 400
    app.commands.put(command_obj)
    return jsonify({"ok": True})


@bp.get("/placeholders.json")
def placeholders():
    """Current values of every heartbeat placeholder, for the payload builder's
    live preview (host metrics + sampled_at included)."""
    from sim_monitor.system.host import collect_host_metrics

    ctx = sim().store.get().placeholder_context()
    ctx.update(collect_host_metrics())
    ctx["sampled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return jsonify(ctx)


@bp.get("/monitor-config.json")
def monitor_config_get():
    """The UI-managed global heartbeat config (secret-free shape + values; this
    lives only on the device DB, never committed)."""
    raw = sim().db.get_setting("monitor")
    return jsonify(raw or MonitorConfig().model_dump(mode="json"))


@bp.put("/monitor-config")
def monitor_config_put():
    body = _body()
    try:
        config = MonitorConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    sim().db.set_setting("monitor", config.model_dump(mode="json"))
    sim().commands.put(cmd.ReloadMonitorConfig())
    return jsonify({"ok": True})


@bp.get("/profiles.json")
def profiles_list():
    app = sim()
    snap = app.store.get()
    profiles, errors = loader.load_profiles(app.config.profiles_dir)
    return jsonify({
        "active": snap.active_profile,
        "forced": snap.forced_profile,
        "profiles": [
            {
                "name": p.name,
                "description": p.description,
                "iccid_patterns": p.match.iccid_patterns,
                "priority": p.match.priority,
                "contexts": [
                    {"cid": c.cid, "apn": c.apn, "pdp_type": c.pdp_type, "bearer": c.bearer}
                    for c in p.pdp_contexts
                ],
                "variants": len(p.pdp_variants),
                "monitor_enabled": p.monitor.enabled,
            }
            for p in profiles
        ],
        "errors": [{"file": e.path.name, "error": e.error} for e in errors],
    })


@bp.get("/profiles/<name>.json")
def profile_get(name: str):
    app = sim()
    path = loader.find_profile_file(app.config.profiles_dir, name)
    if path is None:
        return jsonify({"error": "not found"}), 404
    text = path.read_text(encoding="utf-8")
    # Provide both the structured form data and the raw YAML (escape hatch).
    profile, _ = _validate_yaml(text)
    return jsonify({
        "name": name,
        "yaml": text,
        "profile": profile.model_dump(mode="json") if profile else None,
    })


def _validate_yaml(text: str) -> tuple[Profile | None, str | None]:
    try:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            return None, "profile must be a YAML mapping"
        return Profile.model_validate(raw), None
    except yaml.YAMLError as e:
        return None, f"invalid YAML: {e}"
    except ValidationError as e:
        return None, str(e)


def _profile_from_body(body: dict) -> tuple[Profile | None, str | None]:
    """Accept either a structured {profile: {...}} object (from the form) or
    {yaml: "..."} (the raw editor)."""
    if "profile" in body:
        try:
            return Profile.model_validate(body["profile"]), None
        except ValidationError as e:
            return None, str(e)
    return _validate_yaml(body.get("yaml", ""))


@bp.post("/profiles")
def profile_create():
    app = sim()
    profile, error = _profile_from_body(_body())
    if profile and loader.find_profile_file(app.config.profiles_dir, profile.name):
        error = f"profile {profile.name!r} already exists"
    if error:
        return jsonify({"error": error}), 400
    loader.save_profile(app.config.profiles_dir, profile)
    app.commands.put(cmd.ReloadProfiles())
    return jsonify({"ok": True, "name": profile.name})


@bp.put("/profiles/<name>")
def profile_update(name: str):
    app = sim()
    path = loader.find_profile_file(app.config.profiles_dir, name)
    if path is None:
        return jsonify({"error": "not found"}), 404
    profile, error = _profile_from_body(_body())
    if error:
        return jsonify({"error": error}), 400
    loader.save_profile(app.config.profiles_dir, profile)
    if profile.name != name:
        path.unlink(missing_ok=True)  # renamed: drop the old file
    app.commands.put(cmd.ReloadProfiles())
    return jsonify({"ok": True, "name": profile.name})


@bp.delete("/profiles/<name>")
def profile_delete(name: str):
    app = sim()
    if loader.delete_profile(app.config.profiles_dir, name):
        app.commands.put(cmd.ReloadProfiles())
        return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


def _trigger_update(app):
    """Pull the latest code and reinstall+restart on the device, detached so it
    survives the service restart. Lets you update a Pi from its web UI (no SSH)."""
    import shutil
    import subprocess
    from pathlib import Path

    if app.config.simulate:
        return jsonify({"error": "update is unavailable in simulate mode"}), 400
    source = Path("/etc/sim-monitor/install-source")
    if not source.is_file():
        return jsonify({"error": "install source path unknown; update via git manually"}), 400
    repo = source.read_text(encoding="utf-8").strip()
    script = str(Path(repo) / "deploy" / "self-update.sh")
    if not shutil.which("systemd-run"):
        return jsonify({"error": "systemd-run not available"}), 400
    # Auto-named transient unit (no fixed name to collide on repeated updates);
    # logs are visible via: journalctl -t sim-monitor-update
    subprocess.Popen(  # noqa: S603 - fixed argv, root-owned device, LAN-only
        ["systemd-run", "--no-block", "--collect",
         "--property=SyslogIdentifier=sim-monitor-update", "bash", script, repo]
    )
    return jsonify({
        "ok": True,
        "message": "Update started — the service will restart in ~30–90s. "
                   "Logs: journalctl -t sim-monitor-update",
    })

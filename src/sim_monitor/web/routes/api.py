from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC

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
    from datetime import datetime

    from sim_monitor.monitor.schedule import is_active
    from sim_monitor.system.host import collect_host_metrics

    app = sim()
    data = _snapshot_dict(app)
    last = app.db.recent_monitor_results(limit=1)
    data["last_monitor"] = last[0] if last else None
    # Live uptime: device uptime (Linux /proc) + server clock so the UI can
    # render "connected for N" from state_since without clock-skew guessing.
    data["device_uptime_s"] = collect_host_metrics().get("uptime_s")
    data["server_time"] = time.time()
    # Whether the heartbeat would fire right now: master switch on, an endpoint
    # configured, and inside the schedule window (override-aware).
    cfg = app.daemon.effective_monitor_config()
    data["monitor_active"] = bool(
        cfg and cfg.enabled and cfg.request is not None
        and is_active(cfg.schedule, datetime.now(UTC))
    )
    return jsonify(data)


@bp.get("/connectivity.json")
def connectivity():
    """Cellular-uptime summary + outage episodes for a time window. Query params
    `from`/`to` are epoch seconds; defaults to the last 24h."""
    from sim_monitor.core.uptime import summarize

    db = sim().db
    now = time.time()
    to = request.args.get("to", type=float) or now
    frm = request.args.get("from", type=float) or (to - 86400)
    to = min(to, now)
    if frm > to:
        frm = to
    # Don't count time before we have any data (e.g. just-installed device);
    # clamp the window start to the data horizon so uptime% reflects reality.
    data_since = db.connectivity_first_ts()
    if data_since is not None and frm < data_since:
        frm = min(data_since, to)
    start = db.connectivity_state_at(frm)
    start_up = bool(start["up"]) if start else False
    start_detail = start["detail"] if start else None
    edges = [(r["ts"], bool(r["up"]), r["detail"]) for r in db.connectivity_between(frm, to)]
    summary = summarize(start_up, edges, frm, to, start_detail)
    summary["data_since"] = data_since
    return jsonify(summary)


@bp.get("/latency.json")
def latency():
    """Per-interface latency + packet-loss series for a window. Query params
    `from`/`to` are epoch seconds (default last 24h); optional `interface`
    filters to one. Source resolution auto-scales by window: raw samples for
    short ranges, hourly then daily rollups for longer ones, to keep payloads
    small while still covering ~30 days."""
    from sim_monitor.core.latency import summarize_latency

    db = sim().db
    now = time.time()
    to = request.args.get("to", type=float) or now
    frm = request.args.get("from", type=float) or (to - 86400)
    to = min(to, now)
    if frm > to:
        frm = to
    interface = request.args.get("interface") or None
    span = to - frm
    if span <= 2 * 86400:
        rows = db.icmp_samples_between(frm, to, interface=interface)
        ts_key, source = "ts", "raw"
    elif span <= 14 * 86400:
        rows = db.icmp_rollups_between("hour", frm, to, interface=interface)
        ts_key, source = "bucket_start", "hour"
    else:
        rows = db.icmp_rollups_between("day", frm, to, interface=interface)
        ts_key, source = "bucket_start", "day"
    summary = summarize_latency(rows, frm, to, ts_key=ts_key)
    summary["source"] = source
    summary["cellular_interface"] = sim().store.get().interface
    return jsonify(summary)


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
    source = request.args.get("source") or None
    kind = request.args.get("kind") or None
    limit = max(1, min(request.args.get("limit", 50, type=int), 200))
    offset = max(0, request.args.get("offset", 0, type=int))
    merged = build_timeline(
        events=app.db.recent_events(limit=2000),
        urcs=app.db.recent_urcs(limit=2000),
        identity=app.db.recent_identity(limit=200),
        limit=1_000_000,
    )
    kinds = sorted({r["kind"] for r in merged})  # for the filter dropdown
    if source:
        merged = [r for r in merged if r["source"] == source]
    if kind:
        merged = [r for r in merged if r["kind"] == kind]
    return jsonify({
        "rows": merged[offset:offset + limit],
        "total": len(merged),
        "kinds": kinds,
        "limit": limit,
        "offset": offset,
    })


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
            case "scan-serial-ports":
                command_obj = cmd.ScanSerialPorts()
            case "probe-at-port":
                command_obj = cmd.ProbeAtPort(device=str(body["device"]))
            case "set-at-port":
                command_obj = cmd.SetAtPort(device=str(body.get("device", "")))
            case "set-rat":
                from sim_monitor.modem.driver_base import RAT_LABELS
                rat = str(body.get("rat", ""))
                if rat not in RAT_LABELS:
                    return jsonify({"error": f"unknown RAT {rat!r}"}), 400
                command_obj = cmd.SetRat(rat=rat)
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
    from sim_monitor.system.host import collect_host_metrics, collect_interface_ips

    ctx = sim().store.get().placeholder_context()
    ctx.update(collect_host_metrics())
    ctx.update(collect_interface_ips())
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


@bp.get("/profiles/export.json")
def profiles_export():
    """Download all profiles as one JSON bundle — for copying a device's full
    profile set onto other monitors without committing them to git."""
    import json

    app = sim()
    profiles, _ = loader.load_profiles(app.config.profiles_dir)
    bundle = {
        "schema": "sim-monitor/profiles@1",
        "exported_at": time.time(),
        "profiles": [p.model_dump(mode="json") for p in profiles],
    }
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    fname = f"sim-monitor-profiles-{stamp}.json"
    return Response(
        json.dumps(bundle, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@bp.post("/profiles/import")
def profiles_import():
    """Import profiles from an exported bundle. Adds new ones and overwrites
    same-named ones; other existing profiles are left untouched."""
    app = sim()
    body = _body()
    items = body.get("profiles") if isinstance(body, dict) else body
    if not isinstance(items, list):
        return jsonify({"error": "expected a profiles bundle (or list of profiles)"}), 400
    imported, errors = 0, []
    for raw in items:
        try:
            profile = Profile.model_validate(raw)
        except (ValidationError, TypeError) as e:
            errors.append({"name": (raw or {}).get("name", "?") if isinstance(raw, dict) else "?",
                           "error": str(e)})
            continue
        loader.save_profile(app.config.profiles_dir, profile)
        imported += 1
    if imported:
        app.commands.put(cmd.ReloadProfiles())
    return jsonify({"imported": imported, "errors": errors})


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

from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC

import yaml
from flask import Blueprint, Response, jsonify, request
from pydantic import ValidationError

from sim_monitor import __version__
from sim_monitor.config import loader
from sim_monitor.config.schema import (
    MonitorConfig,
    Profile,
    SmsAutoReplyConfig,
    TcpListenerConfig,
    UdpListenerConfig,
)
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
    # Unread counts for the off-channel arrival badges/toasts (computed at request
    # time, like last_monitor above). SMS rides on the snapshot via sms_unread.
    data["udp_unread"] = app.db.count_unread_udp()
    data["tcp_unread"] = app.db.count_unread_tcp()
    # Whether any heartbeat would fire right now: master switch on, and at least
    # one enabled destination inside its schedule window (override-aware).
    cfg = app.daemon.effective_monitor_config()
    now_utc = datetime.now(UTC)
    data["monitor_active"] = bool(
        cfg and cfg.enabled and any(
            d.enabled and is_active(d.schedule, now_utc) for d in cfg.destinations
        )
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


def _latency_window() -> tuple[float, float, str | None]:
    """Parse from/to/interface query params (epoch seconds; default last 24h)."""
    now = time.time()
    to = request.args.get("to", type=float) or now
    frm = request.args.get("from", type=float) or (to - 86400)
    to = min(to, now)
    if frm > to:
        frm = to
    return frm, to, request.args.get("interface") or None


def _latency_rows(db, frm: float, to: float, interface: str | None):
    """Pick the data source by window size: raw samples for short ranges, then
    hourly, then daily rollups. Returns (rows, ts_key, source)."""
    span = to - frm
    if span <= 2 * 86400:
        return db.icmp_samples_between(frm, to, interface=interface), "ts", "raw"
    if span <= 14 * 86400:
        return db.icmp_rollups_between("hour", frm, to, interface=interface), "bucket_start", "hour"
    return db.icmp_rollups_between("day", frm, to, interface=interface), "bucket_start", "day"


@bp.get("/latency.json")
def latency():
    """Per-interface latency + packet-loss series for a window. Query params
    `from`/`to` are epoch seconds (default last 24h); optional `interface`
    filters to one. Source resolution auto-scales by window: raw samples for
    short ranges, hourly then daily rollups for longer ones, to keep payloads
    small while still covering ~30 days."""
    from sim_monitor.core.latency import summarize_latency

    db = sim().db
    frm, to, interface = _latency_window()
    rows, ts_key, source = _latency_rows(db, frm, to, interface)
    summary = summarize_latency(rows, frm, to, ts_key=ts_key)
    summary["source"] = source
    summary["cellular_interface"] = sim().store.get().interface
    return jsonify(summary)


@bp.get("/latency.csv")
def latency_csv():
    """The window's latency/loss rows as CSV (same source resolution as
    latency.json), for spreadsheet export. One row per (timestamp, interface,
    target)."""
    import csv
    import io

    db = sim().db
    frm, to, interface = _latency_window()
    rows, ts_key, source = _latency_rows(db, frm, to, interface)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ts_iso", "ts_epoch", "source", "interface", "target",
        "sent", "received", "loss_pct", "rtt_avg_ms", "rtt_min_ms", "rtt_max_ms",
    ])
    for r in rows:
        epoch = r[ts_key]
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        writer.writerow([
            iso, f"{epoch:.0f}", source, r["interface"], r["target"],
            r.get("sent"), r.get("received"), r.get("loss_pct"),
            r.get("rtt_avg_ms"), r.get("rtt_min_ms"), r.get("rtt_max_ms"),
        ])
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = f"sim-monitor-latency-{source}-{stamp}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def _http_check_rows(db, frm: float, to: float, interface: str | None):
    """Pick the HTTP-check source by window size (raw -> hourly -> daily), like
    _latency_rows. Raw samples are adapted to the shared metric shape so the same
    summarizer/CSV code applies; rollups are already metric-shaped."""
    from sim_monitor.core.latency import http_sample_to_metric

    span = to - frm
    if span <= 2 * 86400:
        raw = db.http_samples_between(frm, to, interface=interface)
        return [http_sample_to_metric(r) for r in raw], "ts", "raw"
    if span <= 14 * 86400:
        return db.http_rollups_between("hour", frm, to, interface=interface), "bucket_start", "hour"
    return db.http_rollups_between("day", frm, to, interface=interface), "bucket_start", "day"


@bp.get("/http-checks.json")
def http_checks():
    """Per-interface HTTP/website reachability series for a window (web sibling
    of latency.json). Same query params and source auto-scaling; each series
    point also carries the response `status_code`."""
    from sim_monitor.core.latency import summarize_latency

    db = sim().db
    frm, to, interface = _latency_window()
    rows, ts_key, source = _http_check_rows(db, frm, to, interface)
    summary = summarize_latency(rows, frm, to, ts_key=ts_key)
    summary["source"] = source
    summary["cellular_interface"] = sim().store.get().interface
    return jsonify(summary)


@bp.get("/http-checks.csv")
def http_checks_csv():
    """The window's HTTP-check rows as CSV (same source resolution as
    http-checks.json). One row per (timestamp, interface, URL)."""
    import csv
    import io

    db = sim().db
    frm, to, interface = _latency_window()
    rows, ts_key, source = _http_check_rows(db, frm, to, interface)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "ts_iso", "ts_epoch", "source", "interface", "url", "status_code",
        "sent", "received", "loss_pct", "rtt_avg_ms", "rtt_min_ms", "rtt_max_ms",
    ])
    for r in rows:
        epoch = r[ts_key]
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
        writer.writerow([
            iso, f"{epoch:.0f}", source, r["interface"], r["target"],
            r.get("status_code"),
            r.get("sent"), r.get("received"), r.get("loss_pct"),
            r.get("rtt_avg_ms"), r.get("rtt_min_ms"), r.get("rtt_max_ms"),
        ])
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    name = f"sim-monitor-http-checks-{source}-{stamp}.csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@bp.get("/urcs.json")
def urcs():
    return jsonify(sim().db.recent_urcs(limit=300))


def _page_args() -> tuple[int, int]:
    """(limit, offset) from query args, clamped like the monitor history route."""
    limit = max(1, min(request.args.get("limit", 25, type=int), 200))
    offset = max(0, request.args.get("offset", 0, type=int))
    return limit, offset


@bp.get("/sms.json")
def sms():
    db = sim().db
    limit, offset = _page_args()
    return jsonify({
        "results": db.recent_sms(limit=limit, offset=offset),
        "total": db.count_sms(), "limit": limit, "offset": offset,
    })


@bp.get("/udp.json")
def udp_messages():
    db = sim().db
    limit, offset = _page_args()
    return jsonify({
        "results": db.recent_udp_messages(limit=limit, offset=offset),
        "total": db.count_udp_messages(), "limit": limit, "offset": offset,
    })


@bp.get("/tcp.json")
def tcp_messages():
    db = sim().db
    limit, offset = _page_args()
    return jsonify({
        "results": db.recent_tcp_messages(limit=limit, offset=offset),
        "total": db.count_tcp_messages(), "limit": limit, "offset": offset,
    })


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
    from sim_monitor.monitor.http_monitor import (
        http_check_placeholder_context,
        latency_placeholder_context,
        resolve_egress,
    )
    from sim_monitor.system.host import collect_host_metrics, collect_interface_ips

    app = sim()
    snapshot = app.store.get()
    ctx = snapshot.placeholder_context()
    ctx.update(collect_host_metrics())
    ctx.update(collect_interface_ips())
    ctx.update(latency_placeholder_context(app.db, snapshot.interface))
    ctx.update(http_check_placeholder_context(app.db, snapshot.interface))
    # Preview the egress interface using the first enabled destination (the
    # placeholder resolves per-destination at send time).
    raw = app.db.get_setting("monitor")
    try:
        mon_cfg = MonitorConfig.model_validate(raw) if raw else MonitorConfig()
    except ValidationError:
        mon_cfg = MonitorConfig()
    dest = next((d for d in mon_cfg.destinations if d.enabled), None)
    ctx["egress_interface"] = resolve_egress(dest, snapshot) if dest else None
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


@bp.get("/latency-config.json")
def latency_config_get():
    """The effective per-interface latency monitor config: the UI-managed setting
    if saved, else the config.yaml default. Edits hot-reload on the next cycle."""
    app = sim()
    raw = app.db.get_setting("latency")
    return jsonify(raw or app.config.latency.model_dump(mode="json"))


@bp.put("/latency-config")
def latency_config_put():
    from sim_monitor.config.schema import LatencyConfig

    body = _body()
    try:
        config = LatencyConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    app = sim()
    app.db.set_setting("latency", config.model_dump(mode="json"))
    app.events.info(
        "latency",
        f"latency monitor config updated (enabled={config.enabled}, "
        f"interval={config.interval_seconds}s)",
    )
    return jsonify({"ok": True})


@bp.get("/http-checks-config.json")
def http_checks_config_get():
    """The effective HTTP-check monitor config: the UI-managed setting if saved,
    else the config.yaml default. Edits hot-reload on the next cycle."""
    app = sim()
    raw = app.db.get_setting("http_checks")
    return jsonify(raw or app.config.http_checks.model_dump(mode="json"))


@bp.put("/http-checks-config")
def http_checks_config_put():
    from sim_monitor.config.schema import HttpCheckConfig

    body = _body()
    try:
        config = HttpCheckConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    app = sim()
    app.db.set_setting("http_checks", config.model_dump(mode="json"))
    app.events.info(
        "http_check",
        f"http-check monitor config updated (enabled={config.enabled}, "
        f"interval={config.interval_seconds}s, {len(config.targets)} target(s))",
    )
    return jsonify({"ok": True})


@bp.get("/sms-autoreply.json")
def sms_autoreply_get():
    """The UI-managed SMS auto-reply rules (device DB; never committed). Returns
    the saved config, or an empty disabled default when none is set yet."""
    raw = sim().db.get_setting("sms_auto_reply")
    return jsonify(raw or SmsAutoReplyConfig().model_dump(mode="json"))


@bp.put("/sms-autoreply")
def sms_autoreply_put():
    body = _body()
    try:
        config = SmsAutoReplyConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    app = sim()
    app.db.set_setting("sms_auto_reply", config.model_dump(mode="json"))
    app.events.info(
        "sms",
        f"auto-reply config updated (enabled={config.enabled}, "
        f"{len(config.rules)} rule(s))",
    )
    return jsonify({"ok": True})


@bp.get("/udp-config.json")
def udp_config_get():
    """The UI-managed UDP listener/responder config (device DB; never committed).
    Returns the saved config, or an empty disabled default when none is set yet.
    Includes the listener's last-known runtime status under 'status'."""
    app = sim()
    raw = app.db.get_setting("udp_listener")
    config = raw or UdpListenerConfig().model_dump(mode="json")
    return jsonify({**config, "status": app.db.get_setting("udp_status")})


@bp.put("/udp-config")
def udp_config_put():
    body = _body()
    body.pop("status", None)  # tolerate the read-only status field round-tripping back
    try:
        config = UdpListenerConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    app = sim()
    app.db.set_setting("udp_listener", config.model_dump(mode="json"))
    app.events.info(
        "udp",
        f"listener config updated (enabled={config.enabled}, "
        f"ports={config.ports}, {len(config.rules)} rule(s))",
    )
    return jsonify({"ok": True})


@bp.post("/udp/clear")
def udp_clear():
    sim().db.clear_udp_messages()
    return jsonify({"ok": True})


@bp.post("/udp/delete")
def udp_delete():
    sim().db.delete_udp_message(int(_body()["id"]))
    return jsonify({"ok": True})


@bp.post("/udp/mark-read")
def udp_mark_read():
    sim().db.mark_udp_read()
    return jsonify({"ok": True})


@bp.get("/tcp-config.json")
def tcp_config_get():
    """The UI-managed TCP listener/responder config (device DB; never committed).
    Returns the saved config, or an empty disabled default when none is set yet.
    Includes the listener's last-known runtime status under 'status'."""
    app = sim()
    raw = app.db.get_setting("tcp_listener")
    config = raw or TcpListenerConfig().model_dump(mode="json")
    return jsonify({**config, "status": app.db.get_setting("tcp_status")})


@bp.put("/tcp-config")
def tcp_config_put():
    body = _body()
    body.pop("status", None)  # tolerate the read-only status field round-tripping back
    try:
        config = TcpListenerConfig.model_validate(body)
    except ValidationError as e:
        return jsonify({"error": str(e)}), 400
    app = sim()
    app.db.set_setting("tcp_listener", config.model_dump(mode="json"))
    app.events.info(
        "tcp",
        f"listener config updated (enabled={config.enabled}, "
        f"ports={config.ports}, {len(config.rules)} rule(s))",
    )
    return jsonify({"ok": True})


@bp.post("/tcp/clear")
def tcp_clear():
    sim().db.clear_tcp_messages()
    return jsonify({"ok": True})


@bp.post("/tcp/delete")
def tcp_delete():
    sim().db.delete_tcp_message(int(_body()["id"]))
    return jsonify({"ok": True})


@bp.post("/tcp/mark-read")
def tcp_mark_read():
    sim().db.mark_tcp_read()
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

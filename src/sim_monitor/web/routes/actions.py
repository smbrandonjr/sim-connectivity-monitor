from __future__ import annotations

from flask import Blueprint, flash, redirect, request, url_for

from sim_monitor.core import commands as cmd
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("actions", __name__, url_prefix="/actions")


@bp.post("/reconnect")
def reconnect():
    sim().commands.put(cmd.Reconnect())
    flash("reconnect requested", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/reset-modem")
def reset_modem():
    sim().commands.put(cmd.ResetModem())
    flash("modem reset requested", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/monitor-now")
def monitor_now():
    sim().commands.put(cmd.RunMonitorNow())
    flash("heartbeat send requested", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/monitor-pause")
def monitor_pause():
    sim().commands.put(cmd.PauseMonitor())
    flash("pausing scheduled heartbeats", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/monitor-resume")
def monitor_resume():
    sim().commands.put(cmd.ResumeMonitor())
    flash("resuming scheduled heartbeats", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/fallback-test")
def fallback_test():
    raw = request.form.get("duration_seconds", "").strip()
    duration = None
    if raw:
        try:
            duration = max(10, int(raw))
        except ValueError:
            flash("invalid duration", "error")
            return redirect(url_for("status.dashboard"))
    sim().commands.put(cmd.StartFallbackTest(duration_seconds=duration))
    flash("fallback test requested", "ok")
    return redirect(url_for("status.dashboard"))


@bp.post("/fallback-abort")
def fallback_abort():
    sim().commands.put(cmd.AbortFallbackTest())
    flash("fallback test abort requested", "ok")
    return redirect(url_for("status.dashboard"))

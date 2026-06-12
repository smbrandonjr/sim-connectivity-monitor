from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from sim_monitor.core import commands as cmd
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")

MAX_COMMANDS = 20


@bp.get("/")
def page():
    snap = sim().store.get()
    return render_template("diagnostics.html", snap=snap)


@bp.post("/run")
def run():
    raw = request.form.get("commands", "")
    commands = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(commands) > MAX_COMMANDS:
        flash(f"too many commands (max {MAX_COMMANDS})", "error")
        return redirect(url_for("diagnostics.page"))
    bad = [c for c in commands if not c.upper().startswith("AT")]
    if bad:
        flash(f"not AT commands: {', '.join(bad[:3])}", "error")
        return redirect(url_for("diagnostics.page"))
    sim().commands.put(cmd.RunDiagnostics(commands=tuple(commands)))
    flash(
        "diagnostics queued -- results appear below within a few seconds"
        + ("" if commands else " (standard bundle)"),
        "ok",
    )
    return redirect(url_for("diagnostics.page"))

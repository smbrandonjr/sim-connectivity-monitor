from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for

from sim_monitor.core import commands as cmd
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("messages", __name__, url_prefix="/messages")


@bp.get("/")
def inbox():
    app = sim()
    return render_template("messages.html", messages=app.db.recent_sms(limit=200))


@bp.post("/send")
def send():
    number = (request.form.get("number") or "").strip()
    text = request.form.get("text") or ""
    if not number or not text:
        flash("number and message are required", "error")
        return redirect(url_for("messages.inbox"))
    sim().commands.put(cmd.SendSms(number=number, text=text))
    flash(f"sending message to {number}", "ok")
    return redirect(url_for("messages.inbox"))


@bp.post("/<int:row_id>/delete")
def delete(row_id: int):
    sim().commands.put(cmd.DeleteSms(row_id=row_id))
    flash("message deleted", "ok")
    return redirect(url_for("messages.inbox"))


@bp.post("/clear")
def clear():
    sim().commands.put(cmd.ClearSms())
    flash("clearing all messages on the modem", "ok")
    return redirect(url_for("messages.inbox"))


@bp.post("/refresh")
def refresh():
    sim().commands.put(cmd.RefreshSms())
    flash("refreshing inbox from the modem", "ok")
    return redirect(url_for("messages.inbox"))

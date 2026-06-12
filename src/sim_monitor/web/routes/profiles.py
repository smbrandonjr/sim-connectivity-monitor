from __future__ import annotations

import yaml
from flask import Blueprint, flash, redirect, render_template, request, url_for
from pydantic import ValidationError

from sim_monitor.config import loader
from sim_monitor.config.schema import Profile
from sim_monitor.core import commands as cmd
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("profiles", __name__, url_prefix="/profiles")

NEW_PROFILE_TEMPLATE = """\
name: my-profile
description: ""
match:
  iccid_patterns: ["8944500*"]
  priority: 100
pdp_contexts:
  - cid: 1
    apn: hologram
    pdp_type: IPv4
    auth: none
    bearer: true
at_init: []
routing:
  make_default: true
  metric: 50
monitor:
  enabled: false
fallback_test:
  airplane_seconds: 900
"""


def _parse_profile_yaml(text: str) -> tuple[Profile | None, str | None]:
    try:
        raw = yaml.safe_load(text)
        if not isinstance(raw, dict):
            return None, "profile must be a YAML mapping"
        return Profile.model_validate(raw), None
    except yaml.YAMLError as e:
        return None, f"invalid YAML: {e}"
    except ValidationError as e:
        return None, str(e)


@bp.get("/")
def list_profiles():
    app = sim()
    profiles, errors = loader.load_profiles(app.config.profiles_dir)
    snapshot = app.store.get()
    return render_template(
        "profiles.html", profiles=profiles, errors=errors, snap=snapshot
    )


@bp.route("/new", methods=["GET", "POST"])
def new_profile():
    app = sim()
    if request.method == "GET":
        return render_template(
            "profile_form.html", title="New profile", yaml_text=NEW_PROFILE_TEMPLATE,
            action=url_for("profiles.new_profile"),
        )
    text = request.form.get("yaml_text", "")
    profile, error = _parse_profile_yaml(text)
    if profile and loader.find_profile_file(app.config.profiles_dir, profile.name):
        error = f"profile {profile.name!r} already exists"
    if error:
        flash(error, "error")
        return render_template(
            "profile_form.html", title="New profile", yaml_text=text,
            action=url_for("profiles.new_profile"),
        ), 400
    loader.save_profile(app.config.profiles_dir, profile)
    app.commands.put(cmd.ReloadProfiles())
    flash(f"profile {profile.name!r} created", "ok")
    return redirect(url_for("profiles.list_profiles"))


@bp.route("/<name>/edit", methods=["GET", "POST"])
def edit_profile(name: str):
    app = sim()
    path = loader.find_profile_file(app.config.profiles_dir, name)
    if path is None:
        flash(f"profile {name!r} not found", "error")
        return redirect(url_for("profiles.list_profiles"))
    if request.method == "GET":
        return render_template(
            "profile_form.html", title=f"Edit {name}",
            yaml_text=path.read_text(encoding="utf-8"),
            action=url_for("profiles.edit_profile", name=name),
        )
    text = request.form.get("yaml_text", "")
    profile, error = _parse_profile_yaml(text)
    if error:
        flash(error, "error")
        return render_template(
            "profile_form.html", title=f"Edit {name}", yaml_text=text,
            action=url_for("profiles.edit_profile", name=name),
        ), 400
    if profile.name != name:
        # Renamed within the editor: write under the new name, drop the old file.
        loader.save_profile(app.config.profiles_dir, profile)
        path.unlink(missing_ok=True)
    else:
        path.write_text(text, encoding="utf-8")
    app.commands.put(cmd.ReloadProfiles())
    flash(f"profile {profile.name!r} saved", "ok")
    return redirect(url_for("profiles.list_profiles"))


@bp.post("/<name>/delete")
def delete_profile(name: str):
    app = sim()
    if loader.delete_profile(app.config.profiles_dir, name):
        app.commands.put(cmd.ReloadProfiles())
        flash(f"profile {name!r} deleted", "ok")
    else:
        flash(f"profile {name!r} not found", "error")
    return redirect(url_for("profiles.list_profiles"))


@bp.post("/<name>/force")
def force_profile(name: str):
    app = sim()
    app.commands.put(cmd.ForceProfile(name))
    flash(f"forcing profile {name!r}", "ok")
    return redirect(url_for("profiles.list_profiles"))


@bp.post("/release-force")
def release_force():
    app = sim()
    app.commands.put(cmd.ReleaseForce())
    flash("released forced profile; back to ICCID matching", "ok")
    return redirect(url_for("profiles.list_profiles"))

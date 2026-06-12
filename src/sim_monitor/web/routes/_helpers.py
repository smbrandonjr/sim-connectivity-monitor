from __future__ import annotations

from flask import current_app


def sim():
    """The composed sim_monitor.app.App behind this Flask app."""
    return current_app.config["SIM"]

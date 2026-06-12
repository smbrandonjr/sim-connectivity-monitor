"""Flask app factory and waitress runner for the LAN admin UI.

Handlers never touch the modem or network: they read StateStore snapshots and
enqueue commands for the daemon thread.
"""

from __future__ import annotations

import logging
import os
import time

from flask import Flask

log = logging.getLogger(__name__)


def create_app(sim_app) -> Flask:
    """sim_app is the composed sim_monitor.app.App."""
    flask_app = Flask(__name__)
    flask_app.secret_key = os.urandom(24)  # flash messages only; LAN UI, no sessions
    flask_app.config["SIM"] = sim_app

    @flask_app.template_filter("ts")
    def format_timestamp(value: float | None) -> str:
        if not value:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(value))

    from sim_monitor.web.routes import actions, api, diagnostics, logs, profiles, status

    flask_app.register_blueprint(status.bp)
    flask_app.register_blueprint(api.bp)
    flask_app.register_blueprint(profiles.bp)
    flask_app.register_blueprint(actions.bp)
    flask_app.register_blueprint(logs.bp)
    flask_app.register_blueprint(diagnostics.bp)
    return flask_app


def serve(flask_app: Flask, host: str, port: int) -> None:
    from waitress import serve as waitress_serve

    log.info("web UI listening on http://%s:%d", host, port)
    waitress_serve(flask_app, host=host, port=port, threads=4)

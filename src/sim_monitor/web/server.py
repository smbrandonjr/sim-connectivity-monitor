"""Flask app factory and waitress runner for the LAN admin UI.

The UI is a Svelte SPA (served as static files from web/spa) talking to the
JSON API. Handlers never touch the modem or network: they read StateStore
snapshots and enqueue commands for the daemon thread.
"""

from __future__ import annotations

import logging
import os

from flask import Flask

log = logging.getLogger(__name__)


def create_app(sim_app) -> Flask:
    """sim_app is the composed sim_monitor.app.App."""
    flask_app = Flask(__name__)
    flask_app.secret_key = os.urandom(24)
    flask_app.config["SIM"] = sim_app

    from sim_monitor.web.routes import api, spa

    flask_app.register_blueprint(api.bp)
    flask_app.register_blueprint(spa.bp)
    return flask_app


def serve(flask_app: Flask, host: str, port: int) -> None:
    from waitress import serve as waitress_serve

    log.info("web UI listening on http://%s:%d", host, port)
    waitress_serve(flask_app, host=host, port=port, threads=4)

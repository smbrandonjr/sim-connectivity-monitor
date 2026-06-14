"""Serve the built Svelte SPA (committed under web/spa) as static files.

The Pi never needs Node: the dist is built on a dev machine and committed; this
blueprint just hands it out. The SPA uses hash routing, so only '/' and the
hashed asset files need serving.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, send_from_directory

bp = Blueprint("spa", __name__)

SPA_DIR = Path(__file__).resolve().parent.parent / "spa"


@bp.get("/")
def index():
    index_file = SPA_DIR / "index.html"
    if not index_file.is_file():
        abort(503, "SPA not built; run `npm run build` in frontend/")
    return send_from_directory(SPA_DIR, "index.html")


@bp.get("/assets/<path:filename>")
def assets(filename: str):
    return send_from_directory(SPA_DIR / "assets", filename)


@bp.get("/favicon.svg")
def favicon():
    return send_from_directory(SPA_DIR, "favicon.svg")

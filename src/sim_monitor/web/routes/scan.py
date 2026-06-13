from __future__ import annotations

from flask import Blueprint, jsonify, request

from sim_monitor.scan import net
from sim_monitor.web.routes._helpers import sim

bp = Blueprint("scan", __name__, url_prefix="/api")


def _body() -> dict:
    return request.get_json(silent=True) or {}


@bp.get("/scan.json")
def status():
    return jsonify(sim().scan.status())


@bp.get("/scan/interfaces.json")
def interfaces():
    return jsonify(sim().scan.interfaces())


@bp.post("/scan/stop")
def stop():
    sim().scan.stop()
    return jsonify({"ok": True})


def _start(fn) -> tuple:
    try:
        fn()
        return jsonify({"ok": True}), 200
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 409


@bp.post("/scan/discovery")
def discovery():
    body = _body()
    cidr = (body.get("cidr") or "").strip()
    if not cidr:
        return jsonify({"error": "cidr is required"}), 400
    ports = net.parse_ports(body.get("ports", "common"))
    return _start(lambda: sim().scan.start_discovery(cidr, ports))


@bp.post("/scan/ports")
def ports():
    body = _body()
    host = (body.get("host") or "").strip()
    if not host:
        return jsonify({"error": "host is required"}), 400
    port_list = net.parse_ports(body.get("ports", "common"))
    return _start(lambda: sim().scan.start_ports(host, port_list))


@bp.post("/scan/reachability")
def reachability():
    body = _body()
    target = (body.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target is required"}), 400
    return _start(lambda: sim().scan.start_reachability(target, body.get("interface") or None))


@bp.post("/scan/traceroute")
def traceroute():
    body = _body()
    target = (body.get("target") or "").strip()
    if not target:
        return jsonify({"error": "target is required"}), 400
    max_hops = max(1, min(int(body.get("max_hops", 30)), 64))
    return _start(
        lambda: sim().scan.start_traceroute(target, body.get("interface") or None, max_hops)
    )

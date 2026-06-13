"""Pure assembly of the OTA/event timeline and the shareable diagnostic bundle.

These take already-collected data (SQLite rows + a state snapshot) and shape it
for the UI / export. No I/O and no modem access, so the web thread can call
them directly and they're trivially unit-testable.
"""

from __future__ import annotations

from typing import Any


def build_timeline(
    events: list[dict],
    urcs: list[dict],
    identity: list[dict],
    limit: int = 300,
) -> list[dict]:
    """Merge events, URCs, and identity changes into one reverse-chronological
    list. Each entry: {ts, source, kind, detail}."""
    merged: list[dict] = []
    for e in events:
        merged.append(
            {"ts": e["ts"], "source": "event", "kind": e["kind"], "detail": e["message"]}
        )
    for u in urcs:
        merged.append(
            {"ts": u["ts"], "source": "urc", "kind": u["kind"], "detail": u["raw"]}
        )
    for row in identity:
        detail = (
            f"ICCID {row.get('iccid') or '—'} · IMSI {row.get('imsi') or '—'}"
            f" · {row.get('registration') or '—'}"
        )
        merged.append(
            {"ts": row["ts"], "source": "identity", "kind": row["reason"], "detail": detail}
        )
    merged.sort(key=lambda r: r["ts"], reverse=True)
    return merged[:limit]


def build_bundle(
    *,
    generated_at: float,
    app_version: str,
    snapshot: dict[str, Any],
    active_profile: dict[str, Any] | None,
    events: list[dict],
    urcs: list[dict],
    identity: list[dict],
) -> dict:
    """A self-contained JSON diagnostic bundle for sharing/comparison.

    Carries modem identity + firmware, the live status, the active profile
    (secret-free — credentials stripped), and the full event / URC / identity
    history so two devices' OTA behavior can be compared side by side."""
    return {
        "schema": "sim-monitor/diagnostic-bundle@1",
        "generated_at": generated_at,
        "app_version": app_version,
        "modem": {
            "vendor": snapshot.get("vendor"),
            "model": snapshot.get("model"),
            "firmware": snapshot.get("firmware"),
            "imei": snapshot.get("imei"),
        },
        "sim": {
            "iccid": snapshot.get("iccid"),
            "imsi": snapshot.get("imsi"),
            "operator": snapshot.get("operator"),
            "registration": snapshot.get("registration"),
        },
        "status": {
            "state": snapshot.get("state"),
            "interface": snapshot.get("interface"),
            "ip_address": snapshot.get("ip_address"),
            "signal_rssi": snapshot.get("signal_rssi"),
            "signal_percent": snapshot.get("signal_percent"),
            "last_error": snapshot.get("last_error"),
        },
        "active_profile": _strip_secrets(active_profile),
        "identity_history": identity,
        "events": events,
        "urc_log": urcs,
    }


def _strip_secrets(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove APN/monitor credentials so a shared bundle carries no secrets."""
    if profile is None:
        return None
    clean = {k: v for k, v in profile.items() if k not in ("monitor",)}
    contexts = []
    for ctx in profile.get("pdp_contexts", []):
        ctx = dict(ctx)
        if ctx.get("password"):
            ctx["password"] = "***"
        contexts.append(ctx)
    clean["pdp_contexts"] = contexts
    # Keep the monitor's shape but drop URL/headers/body (may carry tokens).
    monitor = profile.get("monitor")
    if monitor:
        clean["monitor"] = {
            "enabled": monitor.get("enabled"),
            "interval_seconds": monitor.get("interval_seconds"),
        }
    return clean

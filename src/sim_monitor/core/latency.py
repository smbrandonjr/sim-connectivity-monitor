"""Pure latency / packet-loss analytics over recorded ICMP samples.

The ping monitor writes one raw row per (interface, target) per probe cycle
(storage.db `icmp_samples`). This module folds raw rows into hourly/daily
aggregate buckets and summarizes a time window into per-interface series the
dashboard charts. No I/O — unit-tested directly, mirroring core.uptime.
"""

from __future__ import annotations

from collections.abc import Iterable

HOUR = 3600.0
DAY = 86400.0
_PERIOD_SECONDS = {"hour": HOUR, "day": DAY}

# Trailing windows exposed as heartbeat placeholders, label -> seconds.
PAYLOAD_WINDOWS = (("1h", 3600), ("3h", 10800), ("6h", 21600), ("24h", 86400))


def period_seconds(period: str) -> float:
    return _PERIOD_SECONDS[period]


def bucket_start(ts: float, period: str) -> float:
    """Floor an epoch timestamp to the start of its hour/day bucket (UTC)."""
    size = _PERIOD_SECONDS[period]
    return (ts // size) * size


def _aggregate(rows: list[dict]) -> dict:
    """Combine raw sample rows into one aggregate. RTT is weighted by received
    packets (each sample's avg already averages its own received pings); loss is
    derived from summed sent/received so cycles with more packets count more."""
    sent = sum(int(r["sent"]) for r in rows)
    received = sum(int(r["received"]) for r in rows)
    loss_pct = round((1 - received / sent) * 100, 2) if sent else 100.0
    # RTT only from samples that actually got replies.
    rtt_rows = [r for r in rows if r.get("rtt_avg_ms") is not None and r["received"]]
    avg = mn = mx = None
    if rtt_rows:
        wsum = sum(r["rtt_avg_ms"] * r["received"] for r in rtt_rows)
        wcount = sum(r["received"] for r in rtt_rows)
        avg = round(wsum / wcount, 2) if wcount else None
        mn = round(min(r["rtt_min_ms"] for r in rtt_rows if r.get("rtt_min_ms") is not None), 2) \
            if any(r.get("rtt_min_ms") is not None for r in rtt_rows) else None
        mx = round(max(r["rtt_max_ms"] for r in rtt_rows if r.get("rtt_max_ms") is not None), 2) \
            if any(r.get("rtt_max_ms") is not None for r in rtt_rows) else None
    return {
        "sample_count": len(rows),
        "sent": sent,
        "received": received,
        "loss_pct": loss_pct,
        "rtt_avg_ms": avg,
        "rtt_min_ms": mn,
        "rtt_max_ms": mx,
    }


def http_sample_to_metric(row: dict) -> dict:
    """Adapt an http_samples/http_rollups row to the icmp-shaped metric dict so
    the shared aggregation/summary code can consume it. A single request is one
    "packet": received iff ok, RTT = request latency (only when ok). The HTTP
    status_code is carried through for display."""
    ok = bool(row.get("ok"))
    lat = row.get("latency_ms") if ok else None
    return {
        "ts": row.get("ts"),
        "bucket_start": row.get("bucket_start"),
        "interface": row["interface"],
        "target": row["target"],
        "sent": 1,
        "received": 1 if ok else 0,
        "loss_pct": 0.0 if ok else 100.0,
        "rtt_avg_ms": lat, "rtt_min_ms": lat, "rtt_max_ms": lat,
        "status_code": row.get("status_code"),
    }


def bucket(samples: Iterable[dict], period: str) -> list[dict]:
    """Group raw samples into (bucket_start, interface, target) aggregates.

    Returns a list of rollup dicts ready for storage.upsert_icmp_rollups,
    sorted by (bucket_start, interface, target)."""
    groups: dict[tuple[float, str, str], list[dict]] = {}
    for s in samples:
        key = (bucket_start(s["ts"], period), s["interface"], s["target"])
        groups.setdefault(key, []).append(s)
    out: list[dict] = []
    for (bstart, interface, target), rows in sorted(groups.items()):
        agg = _aggregate(rows)
        out.append({
            "bucket_start": bstart, "interface": interface, "target": target, **agg,
        })
    return out


def bucket_http(samples: Iterable[dict], period: str) -> list[dict]:
    """Group raw http_samples into (bucket_start, interface, target) aggregates,
    reusing the ICMP numeric aggregation and attaching a representative status
    (the latest non-null in the bucket). Ready for storage.upsert_http_rollups."""
    groups: dict[tuple[float, str, str], list[dict]] = {}
    for s in samples:
        key = (bucket_start(s["ts"], period), s["interface"], s["target"])
        groups.setdefault(key, []).append(s)
    out: list[dict] = []
    for (bstart, interface, target), rows in sorted(groups.items()):
        agg = _aggregate([http_sample_to_metric(r) for r in rows])
        statuses = [(r["ts"], r["status_code"]) for r in rows if r.get("status_code") is not None]
        last_status = max(statuses)[1] if statuses else None
        out.append({
            "bucket_start": bstart, "interface": interface, "target": target,
            "status_code": last_status, **agg,
        })
    return out


def summarize_latency(
    rows: Iterable[dict], t0: float, t1: float, ts_key: str = "ts"
) -> dict:
    """Summarize rows (raw samples or rollups) over [t0, t1] into per-interface
    time series + headline stats.

    `ts_key` is "ts" for raw samples or "bucket_start" for rollups. Each row
    must carry interface, target, loss_pct, rtt_avg_ms/min/max, and (for
    headline weighting) sent/received.

    Series are keyed "<interface>|<target>"; points carry the bucket timestamp,
    loss%, and rtt avg/min/max so the chart can draw a line + min/max band.
    """
    series: dict[str, list[dict]] = {}
    interfaces: list[str] = []
    by_iface_sent: dict[str, int] = {}
    by_iface_recv: dict[str, int] = {}
    by_iface_rtt_wsum: dict[str, float] = {}
    by_iface_rtt_wcount: dict[str, int] = {}

    for r in rows:
        iface = r["interface"]
        if iface not in interfaces:
            interfaces.append(iface)
        key = f"{iface}|{r['target']}"
        point = {
            "ts": r[ts_key],
            "loss_pct": r["loss_pct"],
            "rtt_avg_ms": r.get("rtt_avg_ms"),
            "rtt_min_ms": r.get("rtt_min_ms"),
            "rtt_max_ms": r.get("rtt_max_ms"),
        }
        # HTTP rows carry a status code; harmlessly absent for ICMP rows.
        if "status_code" in r:
            point["status_code"] = r.get("status_code")
        series.setdefault(key, []).append(point)
        sent = int(r.get("sent") or 0)
        recv = int(r.get("received") or 0)
        by_iface_sent[iface] = by_iface_sent.get(iface, 0) + sent
        by_iface_recv[iface] = by_iface_recv.get(iface, 0) + recv
        if r.get("rtt_avg_ms") is not None and recv:
            by_iface_rtt_wsum[iface] = by_iface_rtt_wsum.get(iface, 0.0) + r["rtt_avg_ms"] * recv
            by_iface_rtt_wcount[iface] = by_iface_rtt_wcount.get(iface, 0) + recv

    headline = {}
    for iface in interfaces:
        sent = by_iface_sent.get(iface, 0)
        recv = by_iface_recv.get(iface, 0)
        wcount = by_iface_rtt_wcount.get(iface, 0)
        headline[iface] = {
            "loss_pct": round((1 - recv / sent) * 100, 2) if sent else None,
            "rtt_avg_ms": round(by_iface_rtt_wsum[iface] / wcount, 2) if wcount else None,
            "sent": sent,
            "received": recv,
        }

    for points in series.values():
        points.sort(key=lambda p: p["ts"])

    return {
        "window_start": t0,
        "window_end": t1,
        "interfaces": interfaces,
        "series": series,
        "headline": headline,
    }


def _payload_window(out: dict, prefix: str, suffix: str, rows: list[dict]) -> None:
    """Fill avg/min/max latency + loss for one window into `out`, leaving the
    keys at None when the window has no data (unknown, not zero/100)."""
    if not rows:
        return
    a = _aggregate(rows)
    out[f"{prefix}latency_{suffix}"] = a["rtt_avg_ms"]
    out[f"{prefix}latency_min_{suffix}"] = a["rtt_min_ms"]
    out[f"{prefix}latency_max_{suffix}"] = a["rtt_max_ms"]
    out[f"{prefix}loss_{suffix}"] = a["loss_pct"]


def payload_stats(samples: Iterable[dict], now: float, prefix: str = "") -> dict:
    """Heartbeat placeholders for one interface's latency/loss: the latest probe
    cycle plus trailing 1h/3h/6h/24h windows. `samples` is that interface's raw
    metric rows over the last 24h (across all targets). Always returns every key;
    a value is None when its window has no data, so unknowns are simply omitted
    from the payload.

    For each of {last cycle, 1h, 3h, 6h, 24h} it exposes avg/min/max latency and
    loss. The "last cycle" keys use the bare suffix `ms` (e.g. <prefix>latency_ms,
    <prefix>latency_min_ms, <prefix>latency_max_ms, <prefix>loss_pct); windows use
    the label (e.g. <prefix>latency_24h, <prefix>latency_min_24h, ...). `prefix`
    is "" for ICMP and "http_" for the web-check monitor."""
    out: dict[str, float | None] = {}
    for key in (f"{prefix}latency_ms", f"{prefix}latency_min_ms",
                f"{prefix}latency_max_ms", f"{prefix}loss_pct"):
        out[key] = None
    for label, _ in PAYLOAD_WINDOWS:
        for key in (f"{prefix}latency_{label}", f"{prefix}latency_min_{label}",
                    f"{prefix}latency_max_{label}", f"{prefix}loss_{label}"):
            out[key] = None
    rows = list(samples)
    if not rows:
        return out
    last_ts = max(r["ts"] for r in rows)
    last = _aggregate([r for r in rows if r["ts"] == last_ts])
    out[f"{prefix}latency_ms"] = last["rtt_avg_ms"]
    out[f"{prefix}latency_min_ms"] = last["rtt_min_ms"]
    out[f"{prefix}latency_max_ms"] = last["rtt_max_ms"]
    out[f"{prefix}loss_pct"] = last["loss_pct"]
    for label, win in PAYLOAD_WINDOWS:
        _payload_window(out, prefix, label, [r for r in rows if r["ts"] >= now - win])
    return out

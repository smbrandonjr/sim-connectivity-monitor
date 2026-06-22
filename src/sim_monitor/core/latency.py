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
        series.setdefault(key, []).append({
            "ts": r[ts_key],
            "loss_pct": r["loss_pct"],
            "rtt_avg_ms": r.get("rtt_avg_ms"),
            "rtt_min_ms": r.get("rtt_min_ms"),
            "rtt_max_ms": r.get("rtt_max_ms"),
        })
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

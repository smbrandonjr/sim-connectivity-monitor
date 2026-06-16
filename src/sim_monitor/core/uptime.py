"""Pure connectivity-uptime analytics over recorded state transitions.

The daemon records one row per cellular connected<->down edge (storage.db
`connectivity`). Given the state entering a time window plus the edges inside
it, this computes uptime %, total downtime, and the list of outage episodes for
any timeframe the dashboard asks for. No I/O — unit-tested directly.
"""

from __future__ import annotations

from collections.abc import Iterable


def summarize(
    start_up: bool,
    edges: Iterable[tuple[float, bool, str | None]],
    t0: float,
    t1: float,
    start_detail: str | None = None,
) -> dict:
    """Summarize cellular connectivity over [t0, t1].

    start_up: whether the link was connected at t0.
    edges: ascending (ts, up, detail) transitions within [t0, t1].
    t1 should already be clamped to "now" by the caller for live windows.

    Returns connected/down seconds, uptime %, and outage episodes (down
    intervals with start/end/duration/reason; the last may be `ongoing`).
    """
    if t1 < t0:
        t1 = t0
    connected_s = 0.0
    down_s = 0.0
    episodes: list[dict] = []
    cur_up = start_up
    cursor = t0
    down_start: float | None = None if start_up else t0
    down_detail: str | None = None if start_up else start_detail

    for ts, up, detail in edges:
        ts = min(max(ts, t0), t1)
        seg = ts - cursor
        if cur_up:
            connected_s += seg
        else:
            down_s += seg
        if up != cur_up:
            if up:  # down -> up: close the outage
                if down_start is not None:
                    episodes.append({
                        "start": down_start, "end": ts,
                        "duration_s": ts - down_start, "detail": down_detail,
                        "ongoing": False,
                    })
                down_start = None
                down_detail = None
            else:  # up -> down: open an outage
                down_start = ts
                down_detail = detail
            cur_up = up
        cursor = ts

    seg = t1 - cursor  # tail to the window end
    if cur_up:
        connected_s += seg
    else:
        down_s += seg
        if down_start is not None:
            episodes.append({
                "start": down_start, "end": t1, "duration_s": t1 - down_start,
                "detail": down_detail, "ongoing": True,
            })

    total = connected_s + down_s
    return {
        "window_start": t0,
        "window_end": t1,
        "connected_s": connected_s,
        "down_s": down_s,
        "uptime_pct": (connected_s / total * 100) if total > 0 else None,
        "outage_count": len(episodes),
        "longest_outage_s": max((e["duration_s"] for e in episodes), default=0.0),
        "currently_up": cur_up,
        "episodes": episodes,
    }

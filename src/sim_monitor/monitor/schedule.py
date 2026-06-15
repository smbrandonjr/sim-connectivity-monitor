"""Pure schedule-window logic for the heartbeat monitor.

The monitor can be limited to a weekly time window (e.g. Mon-Fri 9-6 Eastern)
so probes only fire when someone is watching. A manual override forces sending
on or off regardless of the window. This module is I/O-free and unit-tested;
the monitor thread calls is_active() each iteration with the current UTC time,
and the status API uses it to report whether monitoring is live right now.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sim_monitor.config.schema import MonitorSchedule


def _minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def is_active(schedule: MonitorSchedule, now_utc: datetime) -> bool:
    """Whether scheduled probes should fire at ``now_utc`` (an aware UTC time).

    The manual override wins; then, when windowing is enabled, the local weekday
    and time-of-day decide. A window whose end is <= start wraps past midnight.
    An unknown timezone fails open (keep monitoring) -- silently going dark is a
    worse failure than an over-eager probe.
    """
    if schedule.override == "on":
        return True
    if schedule.override == "off":
        return False
    if not schedule.enabled:
        return True
    try:
        tz = ZoneInfo(schedule.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return True
    local = now_utc.astimezone(tz)
    if local.weekday() not in schedule.days:
        return False
    minute = local.hour * 60 + local.minute
    start, end = _minutes(schedule.start), _minutes(schedule.end)
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end  # overnight window wraps midnight

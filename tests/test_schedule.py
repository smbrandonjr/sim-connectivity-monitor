"""Pure monitor schedule-window logic (timezone + override aware)."""

from datetime import UTC, datetime

from sim_monitor.config.schema import MonitorSchedule
from sim_monitor.monitor.schedule import is_active

# Reference instants (UTC). June 2025 is EDT (UTC-4); January 2025 is EST (UTC-5).
WED_JUN_10ET = datetime(2025, 6, 11, 14, 0, tzinfo=UTC)   # Wed 10:00 EDT
WED_JUN_08ET = datetime(2025, 6, 11, 12, 0, tzinfo=UTC)   # Wed 08:00 EDT
WED_JUN_19ET = datetime(2025, 6, 11, 23, 0, tzinfo=UTC)   # Wed 19:00 EDT
SUN_JUN_10ET = datetime(2025, 6, 15, 14, 0, tzinfo=UTC)   # Sun 10:00 EDT


def _sched(**kw):
    return MonitorSchedule(enabled=True, **kw)


class TestWindow:
    def test_inside_weekday_window(self):
        assert is_active(_sched(), WED_JUN_10ET)

    def test_before_window(self):
        assert not is_active(_sched(), WED_JUN_08ET)

    def test_after_window(self):
        assert not is_active(_sched(), WED_JUN_19ET)

    def test_weekend_excluded(self):
        assert not is_active(_sched(), SUN_JUN_10ET)

    def test_start_is_inclusive_end_is_exclusive(self):
        # 13:00 UTC = 09:00 EDT (start, included); 22:00 UTC = 18:00 EDT (end, excluded)
        assert is_active(_sched(), datetime(2025, 6, 11, 13, 0, tzinfo=UTC))
        assert not is_active(_sched(), datetime(2025, 6, 11, 22, 0, tzinfo=UTC))


class TestDst:
    def test_winter_uses_est(self):
        # Wed 2025-01-15 14:00 UTC = 09:00 EST -> inside (start inclusive)
        assert is_active(_sched(), datetime(2025, 1, 15, 14, 0, tzinfo=UTC))
        # 13:00 UTC = 08:00 EST -> before window
        assert not is_active(_sched(), datetime(2025, 1, 15, 13, 0, tzinfo=UTC))


class TestOverrideAndDisabled:
    def test_disabled_always_active(self):
        s = MonitorSchedule(enabled=False)  # 3am Sunday, well outside any window
        assert is_active(s, datetime(2025, 6, 15, 7, 0, tzinfo=UTC))

    def test_override_on_ignores_window(self):
        assert is_active(_sched(override="on"), SUN_JUN_10ET)

    def test_override_off_ignores_window(self):
        assert not is_active(_sched(override="off"), WED_JUN_10ET)


class TestOvernight:
    def test_window_wraps_midnight(self):
        s = _sched(start="22:00", end="02:00", days=[0, 1, 2, 3, 4, 5, 6])
        # 04:00 UTC = 00:00 EDT -> inside the 22:00-02:00 window
        assert is_active(s, datetime(2025, 6, 12, 4, 0, tzinfo=UTC))
        # 12:00 UTC = 08:00 EDT -> outside
        assert not is_active(s, datetime(2025, 6, 12, 12, 0, tzinfo=UTC))


def test_other_timezone():
    s = _sched(timezone="America/Los_Angeles")
    # Wed 17:00 UTC = 10:00 PDT -> inside
    assert is_active(s, datetime(2025, 6, 11, 17, 0, tzinfo=UTC))
    # Wed 14:00 UTC = 07:00 PDT -> before window
    assert not is_active(s, datetime(2025, 6, 11, 14, 0, tzinfo=UTC))

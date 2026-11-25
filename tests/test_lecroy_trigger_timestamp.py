import calendar
from types import SimpleNamespace

import pytest

from lab_scopes.lecroy import wavedesc_trigger_timestamp


def _wd(year, month, day, hour, minute, second):
    return SimpleNamespace(tt_year=year, tt_months=month, tt_days=day,
                           tt_hours=hour, tt_minute=minute, tt_second=second)


@pytest.mark.parametrize("fields, utc", [
    # PDT (UTC-7): 14:00 LA is 21:00 UTC
    ((2026, 7, 15, 14, 0, 12.25), (2026, 7, 15, 21, 0, 12)),
    # PST (UTC-8): 14:00 LA is 22:00 UTC
    ((2026, 1, 15, 14, 0, 12.25), (2026, 1, 15, 22, 0, 12)),
])
def test_scope_fields_are_la_local_time(fields, utc):
    assert wavedesc_trigger_timestamp(_wd(*fields)) == pytest.approx(calendar.timegm(utc) + 0.25)


def test_tz_override_utc():
    assert wavedesc_trigger_timestamp(_wd(2026, 7, 15, 21, 0, 0.0), tz="UTC") == \
        calendar.timegm((2026, 7, 15, 21, 0, 0))


def test_independent_of_host_timezone(monkeypatch):
    import time
    expected = wavedesc_trigger_timestamp(_wd(2026, 7, 15, 14, 0, 0.0))
    monkeypatch.setenv("TZ", "UTC")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        assert wavedesc_trigger_timestamp(_wd(2026, 7, 15, 14, 0, 0.0)) == expected
    finally:
        monkeypatch.undo()
        if hasattr(time, "tzset"):
            time.tzset()


def test_dst_end_repeated_hour_uses_first_occurrence():
    # 2026-11-01 01:30 happens twice in LA; first is PDT (08:30 UTC).
    assert wavedesc_trigger_timestamp(_wd(2026, 11, 1, 1, 30, 0.0)) == \
        calendar.timegm((2026, 11, 1, 8, 30, 0))


def test_unset_or_bad_fields_return_none():
    assert wavedesc_trigger_timestamp(_wd(0, 0, 0, 0, 0, 0.0)) is None
    assert wavedesc_trigger_timestamp(_wd(2026, 13, 1, 0, 0, 0.0)) is None
    assert wavedesc_trigger_timestamp(_wd(2026, 7, 15, 14, 0, 0.0), tz="Not/AZone") is None

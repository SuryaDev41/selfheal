"""Tests for explicit India Standard Time handling."""

from selfheal.time_utils import format_ist, now_ist


def test_now_ist_has_india_offset():
    assert now_ist().utcoffset().total_seconds() == 19800


def test_format_ist_converts_utc_and_legacy_sqlite_values():
    assert format_ist("2026-01-01T00:00:00+00:00") == "2026-01-01 05:30:00 IST"
    assert format_ist("2026-01-01 00:00:00", assume_utc=True) == "2026-01-01 05:30:00 IST"

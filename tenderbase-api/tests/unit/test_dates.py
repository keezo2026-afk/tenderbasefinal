"""Unit tests for date normalization."""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.utils.dates import (
    ensure_utc,
    parse_closing_datetime,
    parse_datetime,
    to_iso,
    utcnow,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-15", (2026, 9, 15)),
        ("15/09/2026", (2026, 9, 15)),
        ("15-09-2026", (2026, 9, 15)),
        ("15 September 2026", (2026, 9, 15)),
        ("15th September 2026", (2026, 9, 15)),
        ("September 15, 2026", (2026, 9, 15)),
        ("Closing date: 15 Sep 2026 at 11:00", (2026, 9, 15)),
    ],
)
def test_parses_common_south_african_date_formats(raw, expected):
    parsed = parse_datetime(raw)
    assert parsed.ok
    # Values are stored in UTC; the published (SAST) calendar date is preserved.
    local = parsed.value.astimezone(ZoneInfo("Africa/Johannesburg"))
    assert (local.year, local.month, local.day) == expected


def test_naive_times_are_interpreted_as_sast_and_stored_utc():
    parsed = parse_datetime("15 September 2026 at 11:00")
    assert parsed.has_time
    # 11:00 SAST (UTC+2) == 09:00 UTC
    assert parsed.value == datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert parsed.source_timezone == "Africa/Johannesburg"


def test_explicit_offset_overrides_assumed_timezone():
    parsed = parse_datetime("2026-09-15T11:00:00+00:00")
    assert parsed.value == datetime(2026, 9, 15, 11, 0, tzinfo=UTC)


def test_h_separated_times_are_supported():
    parsed = parse_datetime("15 September 2026, 11h00")
    assert parsed.value.hour == 9  # 11h00 SAST


def test_am_pm_times():
    parsed = parse_datetime("15 September 2026 2:30 pm")
    assert parsed.value.hour == 12 and parsed.value.minute == 30  # 14:30 SAST


def test_unparseable_values_return_none_and_preserve_raw():
    parsed = parse_datetime("closing date to be confirmed")
    assert parsed.value is None
    assert parsed.raw == "closing date to be confirmed"
    assert not parsed.ok


def test_none_input_is_handled():
    parsed = parse_datetime(None)
    assert parsed.value is None and parsed.raw is None


def test_invalid_calendar_dates_are_rejected():
    assert parse_datetime("31 February 2026").value is None
    assert parse_datetime("2026-13-45").value is None


def test_closing_date_without_time_defaults_to_end_of_day():
    parsed = parse_closing_datetime("15 September 2026")
    assert parsed.value is not None
    assert not parsed.has_time
    # 23:59:59 SAST == 21:59:59 UTC
    assert parsed.value.hour == 21 and parsed.value.minute == 59


def test_datetime_and_date_inputs_pass_through():
    aware = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
    assert parse_datetime(aware).value == aware
    assert parse_datetime(date(2026, 9, 15)).value.astimezone(
        ZoneInfo("Africa/Johannesburg")
    ).date() == date(2026, 9, 15)


def test_ensure_utc_and_to_iso():
    naive = datetime(2026, 9, 15, 11, 0)
    assert ensure_utc(naive).hour == 9
    assert to_iso(ensure_utc(naive)).endswith("Z")
    assert to_iso(None) is None


def test_utcnow_is_timezone_aware():
    assert utcnow().tzinfo is not None

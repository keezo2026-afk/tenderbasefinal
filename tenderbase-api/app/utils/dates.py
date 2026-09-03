"""Date/time normalization.

Everything is normalized to timezone-aware **UTC** internally, while the source
timezone and the original raw string are preserved for auditability.
South African sources overwhelmingly publish local times (SAST, UTC+2) without
an explicit timezone, so ``Africa/Johannesburg`` is the default assumption —
but it is always an explicit, configurable parameter, never a hidden one.

A value that cannot be parsed with confidence returns ``None``. We never invent
dates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import overload
from zoneinfo import ZoneInfo

DEFAULT_SOURCE_TIMEZONE = "Africa/Johannesburg"

_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_ORDINAL_RE = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_AT_RE = re.compile(r"\b(at|@|,)\b", re.IGNORECASE)

# 2026-09-15 / 2026/09/15
_ISO_DATE_RE = re.compile(r"\b(?P<y>\d{4})[-/](?P<m>\d{1,2})[-/](?P<d>\d{1,2})\b")
# 15-09-2026 / 15/09/2026 / 15.09.2026  (day-first: SA convention)
_DMY_RE = re.compile(r"\b(?P<d>\d{1,2})[-/.](?P<m>\d{1,2})[-/.](?P<y>\d{4})\b")
# 15 September 2026
_DMONY_RE = re.compile(r"\b(?P<d>\d{1,2})\s+(?P<mon>[a-z]+)\s+(?P<y>\d{4})\b", re.IGNORECASE)
# September 15, 2026
_MONDY_RE = re.compile(r"\b(?P<mon>[a-z]+)\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})\b", re.IGNORECASE)
# 11:00, 11h00, 11:00:30, 11:00 AM
_TIME_RE = re.compile(
    r"\b(?P<h>\d{1,2})\s*(?::|h)\s*(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>am|pm)?\b",
    re.IGNORECASE,
)
_TZ_OFFSET_RE = re.compile(r"(?P<sign>[+-])(?P<h>\d{2}):?(?P<m>\d{2})$")


@dataclass(frozen=True, slots=True)
class ParsedDateTime:
    """Result of parsing a raw published/closing date string."""

    value: datetime | None
    raw: str | None
    source_timezone: str | None
    has_time: bool = False
    confidence: float = 0.0

    @property
    def ok(self) -> bool:
        return self.value is not None


def utcnow() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(UTC)


@overload
def ensure_utc(value: datetime, *, assume_timezone: str = ...) -> datetime: ...


@overload
def ensure_utc(value: None, *, assume_timezone: str = ...) -> None: ...


def ensure_utc(
    value: datetime | None, *, assume_timezone: str = DEFAULT_SOURCE_TIMEZONE
) -> datetime | None:
    """Return ``value`` as timezone-aware UTC, assuming a timezone when naive.

    ``None`` in, ``None`` out — and never otherwise — so callers that hold a
    non-optional timestamp do not have to re-narrow the result.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(assume_timezone))
    return value.astimezone(UTC)


def _clean(raw: str) -> str:
    text = _ORDINAL_RE.sub("", raw)
    text = text.replace("\u00a0", " ")
    text = _AT_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def _extract_date(text: str) -> date | None:
    if m := _ISO_DATE_RE.search(text):
        return _safe_date(int(m["y"]), int(m["m"]), int(m["d"]))
    if m := _DMY_RE.search(text):
        day, month = int(m["d"]), int(m["m"])
        # Tolerate an unambiguous month-first value (e.g. 09/15/2026).
        if day > 12 >= month or month <= 12:
            return _safe_date(int(m["y"]), month, day) or _safe_date(int(m["y"]), day, month)
    if m := _DMONY_RE.search(text):
        if (named_month := _MONTHS.get(m["mon"].lower())) is not None:
            return _safe_date(int(m["y"]), named_month, int(m["d"]))
    if m := _MONDY_RE.search(text):
        if (named_month := _MONTHS.get(m["mon"].lower())) is not None:
            return _safe_date(int(m["y"]), named_month, int(m["d"]))
    return None


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _extract_time(text: str) -> time | None:
    match = _TIME_RE.search(text)
    if not match:
        return None
    hour = int(match["h"])
    minute = int(match["mi"])
    second = int(match["s"] or 0)
    ampm = (match["ampm"] or "").lower()
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def parse_datetime(
    raw: str | datetime | date | None,
    *,
    source_timezone: str = DEFAULT_SOURCE_TIMEZONE,
    default_time: time | None = None,
) -> ParsedDateTime:
    """Parse a raw date/time value into UTC, preserving the original string.

    Returns a :class:`ParsedDateTime` whose ``value`` is ``None`` when nothing
    could be parsed confidently — callers must persist ``NULL`` rather than
    guessing.
    """
    if raw is None:
        return ParsedDateTime(None, None, None)

    if isinstance(raw, datetime):
        return ParsedDateTime(
            ensure_utc(raw, assume_timezone=source_timezone),
            raw.isoformat(),
            source_timezone if raw.tzinfo is None else str(raw.tzinfo),
            has_time=True,
            confidence=1.0,
        )
    if isinstance(raw, date):
        naive = datetime.combine(raw, default_time or time(0, 0))
        return ParsedDateTime(
            ensure_utc(naive, assume_timezone=source_timezone),
            raw.isoformat(),
            source_timezone,
            has_time=default_time is not None,
            confidence=1.0,
        )

    original = str(raw)
    text = _clean(original)
    if not text:
        return ParsedDateTime(None, original, None)

    # Fast path: a real ISO-8601 timestamp (possibly with offset or "Z").
    iso_candidate = text.replace("Z", "+00:00").replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(iso_candidate)
    except ValueError:
        parsed = None
    if parsed is not None:
        tz_name = str(parsed.tzinfo) if parsed.tzinfo else source_timezone
        return ParsedDateTime(
            ensure_utc(parsed, assume_timezone=source_timezone),
            original,
            tz_name,
            has_time="T" in iso_candidate or bool(_TIME_RE.search(text)),
            confidence=1.0,
        )

    day = _extract_date(text)
    if day is None:
        return ParsedDateTime(None, original, None)

    clock = _extract_time(text)
    has_time = clock is not None
    naive = datetime.combine(day, clock or default_time or time(0, 0))

    tz_name = source_timezone
    if offset := _TZ_OFFSET_RE.search(text.strip()):
        # An explicit numeric offset overrides the assumed source timezone.
        sign = 1 if offset["sign"] == "+" else -1
        minutes = sign * (int(offset["h"]) * 60 + int(offset["m"]))
        from datetime import timedelta, timezone

        tzinfo = timezone(timedelta(minutes=minutes))
        return ParsedDateTime(
            naive.replace(tzinfo=tzinfo).astimezone(UTC),
            original,
            str(tzinfo),
            has_time=has_time,
            confidence=0.9,
        )

    return ParsedDateTime(
        ensure_utc(naive, assume_timezone=tz_name),
        original,
        tz_name,
        has_time=has_time,
        confidence=0.9 if has_time else 0.7,
    )


def parse_closing_datetime(
    raw: str | datetime | date | None, *, source_timezone: str = DEFAULT_SOURCE_TIMEZONE
) -> ParsedDateTime:
    """Parse a closing date.

    When only a date is published, the closing instant is *unknown*; we keep the
    date but flag ``has_time=False`` and use 23:59:59 local so that filters like
    ``closing_after=today`` behave sensibly without pretending precision.
    """
    return parse_datetime(raw, source_timezone=source_timezone, default_time=time(23, 59, 59))


def to_iso(value: datetime | None) -> str | None:
    """Render a datetime as an ISO-8601 UTC string with a ``Z`` suffix."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

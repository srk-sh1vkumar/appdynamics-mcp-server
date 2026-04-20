"""
utils/timezone.py

Timestamp normalisation and display helpers.

Design decisions:
- All timestamps are normalised to UTC internally using python-dateutil,
  which handles ISO8601, RFC2822, epoch integers, and most human formats.
- format_for_display() always shows UTC and annotates the user's local
  timezone alongside it: "2026-04-12 14:30:00 UTC (20:00:00 IST)"
- epoch_ms_to_utc() converts AppD's millisecond timestamps to datetime.
- Controllers store their timezone in controllers.json. This is used for
  correlation (e.g., "same time of day" in golden snapshot scoring).
"""

from __future__ import annotations

from datetime import UTC, datetime

import dateutil.parser
import pytz


def normalize_to_utc(ts: str | int | float | datetime) -> datetime:
    """
    Parse any timestamp format and return a UTC-aware datetime.
    Accepts: ISO8601 string, epoch seconds (int/float), epoch ms (int > 1e10),
             datetime objects (naive assumed UTC, aware converted).
    """
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)

    if isinstance(ts, (int, float)):
        # AppD uses milliseconds; epoch seconds are < 2e10
        epoch_s = ts / 1000 if ts > 1e10 else float(ts)
        return datetime.fromtimestamp(epoch_s, tz=UTC)

    if isinstance(ts, str):
        parsed = dateutil.parser.parse(ts)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    raise TypeError(f"Cannot normalise timestamp of type {type(ts)}")


def epoch_ms_to_utc(epoch_ms: int) -> datetime:
    """Convert AppDynamics millisecond epoch to UTC datetime."""
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def format_for_display(ts: datetime, user_tz: str = "UTC") -> str:
    """
    Return a human-readable timestamp string showing UTC and user's timezone.
    Example: "2026-04-12 14:30:00 UTC (20:00:00 IST)"
    """
    utc_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC")
    if user_tz == "UTC":
        return utc_str
    try:
        local_tz = pytz.timezone(user_tz)
        local_dt = ts.astimezone(local_tz)
        local_str = local_dt.strftime("%H:%M:%S %Z")
        return f"{utc_str} ({local_str})"
    except Exception:
        return utc_str


def format_duration(seconds: float) -> str:
    """Format a duration as 'Xh Ym' or 'Xm Ys'."""
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def same_hour(ts1: datetime, ts2: datetime, tolerance_s: int = 3600) -> bool:
    """True if two timestamps are within tolerance_s of the same time of day."""
    t1 = ts1.hour * 3600 + ts1.minute * 60 + ts1.second
    t2 = ts2.hour * 3600 + ts2.minute * 60 + ts2.second
    return abs(t1 - t2) <= tolerance_s


def same_weekday(ts1: datetime, ts2: datetime) -> bool:
    """True if two timestamps fall on the same day of the week."""
    return ts1.weekday() == ts2.weekday()

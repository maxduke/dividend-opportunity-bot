"""Pure XSHG trading-session helpers for the historical research pipeline.

This module deliberately owns no cache and does not import the production
runtime.  A research run should be reproducible from the same calendar
package version installed in the environment.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from functools import lru_cache

import pandas_market_calendars as mcal

# pandas_market_calendars 5.1.3 has no complete 2026 SSE ad-hoc holiday list
# yet.  These closures are from the official SSE 2026 trading schedule:
# https://www.sse.com.cn/disclosure/announcement/general/c/c_20251222_10802507.shtml
XSHG_2026_CLOSED = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 2),
        *[date(2026, 2, day) for day in range(16, 24)],
        date(2026, 4, 6),
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),
        date(2026, 6, 19), date(2026, 9, 25),
        *[date(2026, 10, day) for day in range(1, 8)],
    }
)


@lru_cache(maxsize=1)
def _xshg_calendar():
    return mcal.get_calendar("XSHG")


def as_date(value: date | datetime | str) -> date:
    """Return a calendar date from the common values used by the pipeline."""

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    raise TypeError(f"unsupported date value: {type(value)!r}")


def xshg_sessions(start: date | datetime | str, end: date | datetime | str) -> list[date]:
    """Return inclusive XSHG sessions in ascending date order."""

    first, last = as_date(start), as_date(end)
    if first > last:
        return []
    valid = _xshg_calendar().valid_days(start_date=first, end_date=last)
    return [stamp.date() for stamp in valid if stamp.date() not in XSHG_2026_CLOSED]


def expected_sessions(start: date | datetime | str, end: date | datetime | str) -> list[date]:
    """Alias used by validation callers."""

    return xshg_sessions(start, end)


def is_xshg_session(value: date | datetime | str) -> bool:
    day = as_date(value)
    return bool(xshg_sessions(day, day))


def session_distance(previous: date | datetime | str, current: date | datetime | str) -> int:
    """Return missing sessions strictly between two observed dates."""

    first, last = as_date(previous), as_date(current)
    if first >= last:
        return 0
    sessions = xshg_sessions(first, last)
    # A boundary may itself be a session, so only interior sessions count.
    return max(0, len(sessions) - int(is_xshg_session(first)) - int(is_xshg_session(last)))


def sessions_between(values: Iterable[date | datetime | str]) -> list[date]:
    """Return inclusive sessions spanning the minimum and maximum value."""

    dates = [as_date(value) for value in values]
    return xshg_sessions(min(dates), max(dates)) if dates else []

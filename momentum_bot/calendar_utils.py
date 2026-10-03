"""NYSE trading calendar helpers (exchange_calendars 'XNYS').

Session dates are plain `datetime.date` values in the exchange's local calendar.
Open/close times are timezone-aware UTC datetimes and include early closes."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

import pandas as pd


@lru_cache(maxsize=1)
def _cal():
    import exchange_calendars as xcals
    return xcals.get_calendar("XNYS", start="2005-01-01")


def _ts(d: date) -> pd.Timestamp:
    return pd.Timestamp(d)


def is_session(d: date) -> bool:
    return bool(_cal().is_session(_ts(d)))


def sessions_in_range(start: date, end: date) -> list[date]:
    if end < start:
        return []
    return [t.date() for t in _cal().sessions_in_range(_ts(start), _ts(end))]


def session_close_utc(d: date) -> datetime:
    return _cal().session_close(_ts(d)).to_pydatetime().astimezone(timezone.utc)


def session_open_utc(d: date) -> datetime:
    return _cal().session_open(_ts(d)).to_pydatetime().astimezone(timezone.utc)


def next_session(d: date) -> date:
    """First session strictly after d (d need not be a session)."""
    return _cal().date_to_session(_ts(d + timedelta(days=1)), direction="next").date()


def previous_session(d: date) -> date:
    """Last session strictly before d."""
    return _cal().date_to_session(_ts(d - timedelta(days=1)), direction="previous").date()


def last_completed_session(now_utc: datetime, delay_minutes: int) -> date:
    """Most recent session whose close (+delay) is at or before now."""
    c = now_utc.astimezone(timezone.utc).date() + timedelta(days=1)
    if not is_session(c):
        c = previous_session(c)
    for _ in range(15):
        if session_close_utc(c) + timedelta(minutes=delay_minutes) <= now_utc:
            return c
        c = previous_session(c)
    raise RuntimeError("could not determine last completed session")


def month_end_sessions(start: date, end: date) -> list[date]:
    """Last session of each calendar month within [start, end] (only full months whose
    last session falls inside the range)."""
    out: dict[tuple[int, int], date] = {}
    for d in sessions_in_range(start, end):
        out[(d.year, d.month)] = d
    result = []
    for (y, m), d in sorted(out.items()):
        # make sure d really is the month's last session (range may cut the month short)
        if next_session(d).month != m or next_session(d).year != y:
            result.append(d)
    return result


def is_last_session_of_month(d: date) -> bool:
    return is_session(d) and next_session(d).month != d.month


def month_end_session_n_months_before(d: date, months: int) -> date:
    """Last session of the calendar month that is `months` months before d's month."""
    y, m = d.year, d.month - months
    while m <= 0:
        m += 12
        y -= 1
    first_of_next = date(y + (m // 12), (m % 12) + 1, 1)
    return previous_session(first_of_next)


def sessions_between(a: date, b: date) -> int:
    """Number of sessions in (a, b]."""
    return len(sessions_in_range(a + timedelta(days=1), b))

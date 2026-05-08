"""Timezone-anchored datetime helpers for the live trading path.

The bot runs on a UTC GitHub Actions runner but trades on the NYSE
calendar (US/Eastern). Any use of naive ``date.today()`` /
``datetime.now()`` in live code is a latent bug: during the
00:00-04:00 UTC window the system date is one day ahead of the ET
trading date, which silently shifts cutoffs in rolling-window queries
and other calendar-anchored logic.

Use these helpers everywhere in the live path. The asyncio/datetime
guard in ``scripts/check_live_path.sh`` enforces this in CI.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from trading_bot.constants import TZ_EASTERN
from trading_bot.data.holiday_calendar import HolidayCalendar

__all__ = ["trading_today", "trading_now", "count_trading_days_between"]


def trading_today() -> date:
    """Return the current ET trading-calendar date.

    Use this in place of ``date.today()`` and ``datetime.now().date()``
    in any code that runs on the live tick or compares against the
    ``daily_summaries`` / trade tables.
    """
    return datetime.now(tz=TZ_EASTERN).date()


def trading_now() -> datetime:
    """Return the current ET-aware datetime.

    Use this in place of ``datetime.now()`` (no ``tz=``) in any code
    that runs on the live tick.
    """
    return datetime.now(tz=TZ_EASTERN)


def count_trading_days_between(
    cal: HolidayCalendar,
    start_date: date,
    end_date: date,
) -> int:
    """Count NYSE trading days strictly between *start_date* (entry)
    and *end_date* (now), exclusive of the entry date and inclusive
    of any session ending on or before *end_date*.

    Examples (no holidays, no weekends shown):
    - entry Mon, now Mon → 0 (same session, intraday — the swing
      branch shouldn't fire).
    - entry Mon, now Tue → 1.
    - entry Thu, now Mon → 2 (Fri + Mon).
    - entry Thu, now Tue → 3 (Fri + Mon + Tue).

    Returns 0 when ``end_date <= start_date``. The walk is bounded
    by calendar-day distance so a corrupted timestamp can't loop
    forever.

    Used by ``ExitManager.check_time_stop`` swing branch (PR #83) and
    the property tests in ``test_property_critical_math.py``. Lives
    here rather than as an instance method on ``ExitManager`` so it
    can be exercised without the surrounding Config + market_data
    plumbing.
    """
    if end_date <= start_date:
        return 0
    count: int = 0
    cursor: date = start_date + timedelta(days=1)
    while cursor <= end_date:
        if cal.is_trading_day(cursor):
            count += 1
        cursor += timedelta(days=1)
    return count

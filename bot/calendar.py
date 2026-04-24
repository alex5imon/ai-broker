from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("XNYS")


def nyse_is_open(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    schedule = _NYSE.schedule(
        start_date=(now - timedelta(days=1)).date(),
        end_date=(now + timedelta(days=1)).date(),
    )
    for _, row in schedule.iterrows():
        if row["market_open"] <= now <= row["market_close"]:
            return True
    return False


def last_bar_timestamp(now: datetime | None = None, interval_minutes: int = 15) -> datetime:
    """Snap `now` down to the previous bar boundary. Used as an idempotency key."""
    now = now or datetime.now(timezone.utc)
    minute = (now.minute // interval_minutes) * interval_minutes
    return now.replace(minute=minute, second=0, microsecond=0)

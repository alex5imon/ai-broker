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

from datetime import date, datetime

from trading_bot.constants import TZ_EASTERN

__all__ = ["trading_today", "trading_now"]


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

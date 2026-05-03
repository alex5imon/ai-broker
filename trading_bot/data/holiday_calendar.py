"""NYSE holiday calendar wrapper for the calendar-effect overlay.

The repo already declares NYSE holidays in ``config.yaml`` under
``holidays.us_<year>``. This module wraps that source plus an in-package
multi-year fallback so the overlay can answer:

* ``is_holiday(d)`` — closed-session day?
* ``is_trading_day(d)`` — weekday and not a holiday?
* ``next_trading_day(d)`` / ``prev_trading_day(d)``

Keeping the wrapper config-aware mirrors the pattern in
``event_calendar.py`` and avoids pulling in ``pandas_market_calendars``
as a new dependency for one small calendar lookup.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


# NYSE full-close holidays. Update annually; config can override.
_FALLBACK_HOLIDAYS: dict[int, list[str]] = {
    2020: [
        "2020-01-01", "2020-01-20", "2020-02-17", "2020-04-10",
        "2020-05-25", "2020-07-03", "2020-09-07", "2020-11-26",
        "2020-12-25",
    ],
    2021: [
        "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02",
        "2021-05-31", "2021-07-05", "2021-09-06", "2021-11-25",
        "2021-12-24",
    ],
    2022: [
        "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30",
        "2022-06-20", "2022-07-04", "2022-09-05", "2022-11-24",
        "2022-12-26",
    ],
    2023: [
        "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07",
        "2023-05-29", "2023-06-19", "2023-07-04", "2023-09-04",
        "2023-11-23", "2023-12-25",
    ],
    2024: [
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29",
        "2024-05-27", "2024-06-19", "2024-07-04", "2024-09-02",
        "2024-11-28", "2024-12-25",
    ],
    2025: [
        "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17",
        "2025-04-18", "2025-05-26", "2025-06-19", "2025-07-04",
        "2025-09-01", "2025-11-27", "2025-12-25",
    ],
    2026: [
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03",
        "2026-05-25", "2026-06-19", "2026-07-03", "2026-09-07",
        "2026-11-26", "2026-12-25",
    ],
    2027: [
        "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26",
        "2027-05-31", "2027-06-18", "2027-07-05", "2027-09-06",
        "2027-11-25", "2027-12-24",
    ],
}


class HolidayCalendar:
    """NYSE holiday calendar with optional config-driven override."""

    def __init__(self, raw_config: dict[str, Any] | None = None) -> None:
        self._raw_config: dict[str, Any] = raw_config or {}
        self._cache: set[date] = set()
        self._loaded_years: set[int] = set()

    def _load_year(self, year: int) -> None:
        if year in self._loaded_years:
            return
        section: dict[str, Any] = self._raw_config.get("holidays", {}) or {}
        cfg_dates: list[Any] | None = section.get(f"us_{year}")
        if cfg_dates:
            for s in cfg_dates:
                try:
                    self._cache.add(date.fromisoformat(str(s)))
                except ValueError:
                    logger.warning("Invalid holiday date in config: %r", s)
        else:
            for s in _FALLBACK_HOLIDAYS.get(year, []):
                self._cache.add(date.fromisoformat(s))
        self._loaded_years.add(year)

    def is_holiday(self, d: date) -> bool:
        self._load_year(d.year)
        return d in self._cache

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and not self.is_holiday(d)

    def next_trading_day(self, d: date) -> date:
        nxt: date = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def prev_trading_day(self, d: date) -> date:
        prv: date = d - timedelta(days=1)
        while not self.is_trading_day(prv):
            prv -= timedelta(days=1)
        return prv

    def days_until_next_session(self, d: date) -> int:
        """Calendar days from *d* (inclusive end of session) to the next
        open session. ``1`` for a normal Mon–Thu, ``3`` for a Friday into
        a normal Monday, ≥ 4 for a long weekend.
        """
        nxt: date = self.next_trading_day(d)
        return (nxt - d).days

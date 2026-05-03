"""FOMC announcement-day calendar — hardcoded multi-year list.

Used by the calendar-effect overlay (``calendar_overlay.is_fomc_window``)
to detect the 24h drift window before each scheduled FOMC announcement.

The legacy gate in ``event_calendar.py`` reads FOMC dates from
``config.yaml`` for a single-year skip/reduce action. This module is the
multi-year source for the *overlay* and does not duplicate the config
plumbing — overlays are a research-only knob, so a hardcoded list keeps
the code path stable across years and easier to audit.

Refresh procedure (annual, target ~ September each year):

    gh issue create -t "Refresh FOMC dates for $(date +%Y+1)" \
        -b "Update _FOMC_DATES in trading_bot/data/fomc_calendar.py from \
            https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"

Source: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
Each year holds 8 scheduled meetings. Announcement day = the second day
of each two-day meeting (post-statement Wednesday for most meetings).
"""

from __future__ import annotations

import logging
from datetime import date

logger: logging.Logger = logging.getLogger(__name__)


_FOMC_DATES: list[str] = [
    # 2020
    "2020-01-29", "2020-03-18", "2020-04-29", "2020-06-10",
    "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    # 2021
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16",
    "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    # 2022
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15",
    "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    # 2023
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14",
    "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    # 2024
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12",
    "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    # 2025
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
    "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    # 2026
    "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
    # 2027 — placeholder until Fed publishes; refresh annually.
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-16",
    "2027-07-28", "2027-09-22", "2027-11-03", "2027-12-15",
]


def get_fomc_dates() -> list[date]:
    """Return all known FOMC announcement dates as ``date`` objects."""
    return [date.fromisoformat(d) for d in _FOMC_DATES]


def log_staleness(today: date) -> None:
    """Emit a startup log line so missing future dates are visible."""
    future: list[date] = [d for d in get_fomc_dates() if d >= today]
    logger.info(
        "FOMC dates loaded: %d future dates remaining (last=%s)",
        len(future),
        future[-1].isoformat() if future else "n/a",
    )

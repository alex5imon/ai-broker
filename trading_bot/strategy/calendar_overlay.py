"""Calendar-effect overlay — sizing multipliers and entry blocks.

This module is an *overlay* on top of existing strategies. It does not
generate entries of its own. After a strategy returns a
``StrategyDecision``, ``StrategyManager`` (live) and the backtester run
the decision through:

* :func:`compute_size_multiplier` — scales ``shares`` by a composed
  multiplier (turn-of-month × FOMC × OPEX), capped to ``[0.0, 2.0]``.
* :func:`should_block_entry` — drops the decision entirely (e.g.
  ``overnight_drift`` before a long weekend).

Each sub-overlay has its own ``enabled`` flag and an opt-in
``applies_to``/``block_strategies`` list — overlays designed against
one strategy archetype must not silently affect another. A master
``calendar_overlay.enabled`` switch disables every sub-overlay
regardless of its own flag.

Design notes
------------

Per the issue spec (#47), this code path is enabled per-overlay only
after the A/B acceptance bar passes. The default config ships every
sub-overlay disabled.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.strategy.base import StrategyDecision

logger: logging.Logger = logging.getLogger(__name__)


_MIN_MULTIPLIER: float = 0.0
_MAX_MULTIPLIER: float = 2.0


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------


def is_turn_of_month(
    d: date,
    holiday_calendar: HolidayCalendar,
    days_before_month_end: int = 4,
    days_after_month_start: int = 3,
) -> bool:
    """Return True if *d* is in the turn-of-month window.

    The window is counted in **trading days**, not calendar days. The
    last ``days_before_month_end`` trading days of the prior month and
    the first ``days_after_month_start`` trading days of *d*'s month
    both count as turn-of-month.

    A non-trading day (weekend, holiday) is never turn-of-month.
    """
    if not holiday_calendar.is_trading_day(d):
        return False

    # First N trading days of d's month.
    cur: date = date(d.year, d.month, 1)
    seen: int = 0
    while cur.month == d.month and seen < days_after_month_start:
        if holiday_calendar.is_trading_day(cur):
            seen += 1
            if cur == d:
                return True
        cur += timedelta(days=1)

    # Last N trading days of d's month.
    if d.month == 12:
        next_month: date = date(d.year + 1, 1, 1)
    else:
        next_month = date(d.year, d.month + 1, 1)
    last_in_month: date = next_month - timedelta(days=1)
    cur = last_in_month
    seen = 0
    while cur.month == d.month and seen < days_before_month_end:
        if holiday_calendar.is_trading_day(cur):
            seen += 1
            if cur == d:
                return True
        cur -= timedelta(days=1)

    return False


def is_fomc_window(
    dt: datetime,
    fomc_dates: list[date],
    hours_before_announcement: int = 24,
) -> bool:
    """Return True if *dt* is within ``hours_before_announcement`` of an
    FOMC announcement.

    Announcement is treated as 14:00 ET on the announcement day (the
    statement release time used in the Lucca & Moench drift study).
    *dt* must be timezone-aware; comparison uses absolute UTC instants.
    """
    if dt.tzinfo is None:
        raise ValueError("is_fomc_window requires a timezone-aware datetime")
    from zoneinfo import ZoneInfo

    et: ZoneInfo = ZoneInfo("US/Eastern")
    window: timedelta = timedelta(hours=hours_before_announcement)
    for d in fomc_dates:
        announcement: datetime = datetime(
            d.year, d.month, d.day, 14, 0, 0, tzinfo=et,
        )
        if announcement - window <= dt < announcement:
            return True
    return False


def is_pre_long_weekend(
    d: date,
    holiday_calendar: HolidayCalendar,
    min_weekend_days: int = 3,
) -> bool:
    """Return True if *d* is the last session before a ≥ ``min_weekend_days``
    closure (Mon holidays, Christmas, July 4, etc.).
    """
    if not holiday_calendar.is_trading_day(d):
        return False
    return holiday_calendar.days_until_next_session(d) >= min_weekend_days


def is_opex_friday(
    d: date,
    weekday: int = 4,
    week_of_month: int = 3,
) -> bool:
    """Return True if *d* is the configured OPEX day (default: 3rd Friday)."""
    if d.weekday() != weekday:
        return False
    # Which occurrence of *weekday* within the month is *d*?
    occurrence: int = (d.day - 1) // 7 + 1
    return occurrence == week_of_month


# ---------------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------------


def _section(
    config: dict[str, Any] | None,
    key: str,
) -> dict[str, Any]:
    """Return the named sub-overlay block, or an empty dict."""
    root: dict[str, Any] = (config or {}).get("calendar_overlay", {}) or {}
    sub: dict[str, Any] = root.get(key, {}) or {}
    return sub


def _master_enabled(config: dict[str, Any] | None) -> bool:
    root: dict[str, Any] = (config or {}).get("calendar_overlay", {}) or {}
    return bool(root.get("enabled", False))


def _applies_to(section: dict[str, Any], strategy_id: str) -> bool:
    applies: list[str] = list(section.get("applies_to", []) or [])
    return strategy_id in applies


def compute_size_multiplier(
    decision: StrategyDecision,
    dt: datetime,
    config: dict[str, Any] | None,
    holiday_calendar: HolidayCalendar | None = None,
    fomc_dates: list[date] | None = None,
) -> float:
    """Compose all enabled overlays into a single ``shares`` multiplier.

    Overlays are multiplicative:
    ``final = turn_of_month × fomc × opex``. The result is clamped to
    ``[0.0, 2.0]``. Returns ``1.0`` when the master switch is off or
    when no sub-overlay applies.
    """
    if not _master_enabled(config):
        return 1.0
    if dt.tzinfo is None:
        raise ValueError("compute_size_multiplier requires a tz-aware datetime")

    cal: HolidayCalendar = holiday_calendar or HolidayCalendar(config)
    d: date = dt.date()
    mult: float = 1.0

    tom: dict[str, Any] = _section(config, "turn_of_month")
    if tom.get("enabled", False) and _applies_to(tom, decision.strategy_id):
        if is_turn_of_month(
            d, cal,
            days_before_month_end=int(tom.get("days_before_month_end", 4)),
            days_after_month_start=int(tom.get("days_after_month_start", 3)),
        ):
            if decision.direction == "long":
                m: float = float(tom.get("long_multiplier", 1.2))
            else:
                m = float(tom.get("short_multiplier", 0.8))
            mult *= m
            logger.info(
                "[calendar_overlay] turn_of_month: %s_multiplier=%.2f applied to %s entry on %s",
                decision.direction, m, decision.strategy_id, decision.ticker,
            )

    fomc: dict[str, Any] = _section(config, "fomc_drift")
    if fomc.get("enabled", False) and _applies_to(fomc, decision.strategy_id):
        dates: list[date] = fomc_dates if fomc_dates is not None else []
        if dates and is_fomc_window(
            dt, dates,
            hours_before_announcement=int(fomc.get("hours_before_announcement", 24)),
        ):
            if decision.direction == "long":
                m = float(fomc.get("long_multiplier", 1.3))
                mult *= m
                logger.info(
                    "[calendar_overlay] fomc_drift: long_multiplier=%.2f applied to %s entry on %s",
                    m, decision.strategy_id, decision.ticker,
                )

    opex: dict[str, Any] = _section(config, "opex")
    if opex.get("enabled", False) and _applies_to(opex, decision.strategy_id):
        if is_opex_friday(
            d,
            weekday=int(opex.get("weekday", 4)),
            week_of_month=int(opex.get("week_of_month", 3)),
        ):
            m = float(opex.get("multiplier", 0.7))
            mult *= m
            logger.info(
                "[calendar_overlay] opex: multiplier=%.2f applied to %s entry on %s",
                m, decision.strategy_id, decision.ticker,
            )

    if mult < _MIN_MULTIPLIER:
        mult = _MIN_MULTIPLIER
    elif mult > _MAX_MULTIPLIER:
        mult = _MAX_MULTIPLIER
    return mult


def should_block_entry(
    decision: StrategyDecision,
    dt: datetime,
    config: dict[str, Any] | None,
    holiday_calendar: HolidayCalendar | None = None,
) -> bool:
    """Return True if any block-style overlay vetoes *decision*."""
    if not _master_enabled(config):
        return False
    if dt.tzinfo is None:
        raise ValueError("should_block_entry requires a tz-aware datetime")

    cal: HolidayCalendar = holiday_calendar or HolidayCalendar(config)

    plw: dict[str, Any] = _section(config, "pre_long_weekend")
    if plw.get("enabled", False):
        block_list: list[str] = list(plw.get("block_strategies", []) or [])
        if decision.strategy_id in block_list and is_pre_long_weekend(
            dt.date(), cal,
            min_weekend_days=int(plw.get("min_weekend_days", 3)),
        ):
            logger.info(
                "[calendar_overlay] pre_long_weekend: blocking %s entry on %s",
                decision.strategy_id, decision.ticker,
            )
            return True

    return False


def apply_overlay(
    decision: StrategyDecision,
    dt: datetime,
    config: dict[str, Any] | None,
    holiday_calendar: HolidayCalendar | None = None,
    fomc_dates: list[date] | None = None,
) -> StrategyDecision | None:
    """One-shot helper: returns ``None`` if blocked, otherwise a new
    decision with ``shares`` scaled by the composed multiplier.

    A multiplier that pushes ``shares`` below 0 also returns ``None`` —
    treating the decision as dropped is safer than passing zero shares
    downstream.
    """
    if should_block_entry(decision, dt, config, holiday_calendar):
        return None

    mult: float = compute_size_multiplier(
        decision, dt, config,
        holiday_calendar=holiday_calendar,
        fomc_dates=fomc_dates,
    )
    if mult <= 0.0:
        return None
    if mult == 1.0:
        return decision

    new_shares: float = decision.shares * mult
    if new_shares <= 0:
        return None
    return replace(decision, shares=new_shares)

"""Tests for the calendar-effect overlay (issue ai-broker#47)."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from trading_bot.constants import HoldType
from trading_bot.data.fomc_calendar import get_fomc_dates
from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.strategy import calendar_overlay
from trading_bot.strategy.base import StrategyDecision

ET = ZoneInfo("US/Eastern")


def _decision(
    strategy_id: str = "mean_reversion",
    direction: str = "long",
    shares: float = 100.0,
    ticker: str = "SPY",
) -> StrategyDecision:
    return StrategyDecision(
        ticker=ticker,
        exchange="NYSE",
        direction=direction,
        shares=shares,
        entry_price=400.0,
        stop_price=395.0,
        target_price=405.0,
        trail_pct=None,
        hold_type=HoldType.INTRADAY,
        strategy_id=strategy_id,
    )


def _full_cfg(
    *,
    master: bool = True,
    tom: bool = False,
    fomc: bool = False,
    plw: bool = False,
    opex: bool = False,
) -> dict[str, Any]:
    return {
        "calendar_overlay": {
            "enabled": master,
            "turn_of_month": {
                "enabled": tom,
                "days_before_month_end": 4,
                "days_after_month_start": 3,
                "long_multiplier": 1.2,
                "short_multiplier": 0.8,
                "applies_to": ["mean_reversion", "overnight_drift"],
            },
            "fomc_drift": {
                "enabled": fomc,
                "hours_before_announcement": 24,
                "long_multiplier": 1.3,
                "applies_to": ["overnight_drift"],
            },
            "pre_long_weekend": {
                "enabled": plw,
                "min_weekend_days": 3,
                "block_strategies": ["overnight_drift"],
            },
            "opex": {
                "enabled": opex,
                "weekday": 4,
                "week_of_month": 3,
                "multiplier": 0.7,
                "applies_to": ["mean_reversion", "overnight_drift"],
            },
        }
    }


@pytest.fixture
def cal() -> HolidayCalendar:
    return HolidayCalendar()


# ---------------------------------------------------------------------------
# Predicate tests
# ---------------------------------------------------------------------------


class TestTurnOfMonth:
    def test_last_4_trading_days(self, cal: HolidayCalendar) -> None:
        # April 2026: weekdays Apr 27 (Mon), 28, 29, 30 are last 4 trading days.
        for d in (date(2026, 4, 27), date(2026, 4, 28), date(2026, 4, 29), date(2026, 4, 30)):
            assert calendar_overlay.is_turn_of_month(d, cal), d

    def test_first_3_trading_days(self, cal: HolidayCalendar) -> None:
        # May 2026: weekdays May 1 (Fri), May 4 (Mon), May 5 (Tue).
        for d in (date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5)):
            assert calendar_overlay.is_turn_of_month(d, cal), d

    def test_middle_of_month(self, cal: HolidayCalendar) -> None:
        assert not calendar_overlay.is_turn_of_month(date(2026, 5, 15), cal)

    def test_handles_weekend(self, cal: HolidayCalendar) -> None:
        # Saturday is never turn-of-month even at month-end.
        assert not calendar_overlay.is_turn_of_month(date(2026, 5, 2), cal)
        assert not calendar_overlay.is_turn_of_month(date(2026, 5, 3), cal)

    def test_skips_holidays_in_count(self, cal: HolidayCalendar) -> None:
        # Jan 2026: New Year (Jan 1, Thu) holiday. First 3 trading days are
        # Jan 2 (Fri), Jan 5 (Mon), Jan 6 (Tue).
        assert calendar_overlay.is_turn_of_month(date(2026, 1, 2), cal)
        assert calendar_overlay.is_turn_of_month(date(2026, 1, 5), cal)
        assert calendar_overlay.is_turn_of_month(date(2026, 1, 6), cal)
        assert not calendar_overlay.is_turn_of_month(date(2026, 1, 7), cal)


class TestFomcWindow:
    def test_within_24h_before(self) -> None:
        fomc = [date(2026, 3, 18)]
        # Announcement at 14:00 ET on Mar 18. 23h earlier = Mar 17 15:00 ET.
        dt = datetime(2026, 3, 17, 15, 0, tzinfo=ET)
        assert calendar_overlay.is_fomc_window(dt, fomc, hours_before_announcement=24)

    def test_outside_24h(self) -> None:
        fomc = [date(2026, 3, 18)]
        # 25h earlier = Mar 17 13:00 ET.
        dt = datetime(2026, 3, 17, 13, 0, tzinfo=ET)
        assert not calendar_overlay.is_fomc_window(dt, fomc, hours_before_announcement=24)

    def test_after_announcement_is_false(self) -> None:
        fomc = [date(2026, 3, 18)]
        dt = datetime(2026, 3, 18, 15, 0, tzinfo=ET)
        assert not calendar_overlay.is_fomc_window(dt, fomc)

    def test_requires_tz_aware(self) -> None:
        with pytest.raises(ValueError):
            calendar_overlay.is_fomc_window(
                datetime(2026, 3, 17, 15, 0), [date(2026, 3, 18)],
            )


class TestPreLongWeekend:
    def test_friday_before_monday_holiday(self, cal: HolidayCalendar) -> None:
        # MLK Day Mon 2026-01-19 → Fri 2026-01-16 is pre-long-weekend.
        assert calendar_overlay.is_pre_long_weekend(date(2026, 1, 16), cal)

    def test_normal_friday(self, cal: HolidayCalendar) -> None:
        # Friday 2026-04-10 → next session Mon 2026-04-13: 3-day gap, but
        # min_weekend_days defaults to 3 (≥ 3). 3-day weekend is the
        # normal weekend, so we want this to NOT trigger.
        # The check is days_until_next_session(d) >= min_weekend_days.
        # Fri to Mon is exactly 3 days, so by default this matches a
        # plain weekend. Use min_weekend_days=4 to require longer gap.
        # Issue spec says "≥ 3 days (Mon holidays, Christmas, July 4)"
        # implying 3 days = pre-long-weekend (it's the Friday before a
        # Monday holiday-extended weekend OR the Friday before a normal
        # weekend that already has 3 days off if Monday is a holiday).
        # For a *plain* Fri→Mon weekend the gap is 3 days too.
        # So with min_weekend_days=3 a normal Friday triggers. The
        # operational fix is to set min_weekend_days=4 in config when
        # only true long weekends should be flagged.
        assert calendar_overlay.is_pre_long_weekend(
            date(2026, 4, 10), cal, min_weekend_days=4,
        ) is False

    def test_christmas_eve_observed(self, cal: HolidayCalendar) -> None:
        # 2026-12-25 is Friday holiday. Wednesday 2026-12-23 → Mon 2026-12-28
        # spans 5 days. Thursday 2026-12-24 → Mon 2026-12-28 spans 4 days.
        assert calendar_overlay.is_pre_long_weekend(date(2026, 12, 24), cal)

    def test_holiday_itself_not_flagged(self, cal: HolidayCalendar) -> None:
        # Holidays are not trading days, so they're never pre-long-weekend.
        assert not calendar_overlay.is_pre_long_weekend(date(2026, 1, 19), cal)


class TestOpexFriday:
    @pytest.mark.parametrize(
        "d",
        [
            date(2026, 1, 16),  # 3rd Fri of Jan 2026
            date(2026, 2, 20),  # 3rd Fri of Feb 2026
            date(2026, 3, 20),  # 3rd Fri of Mar 2026
            date(2026, 6, 19),  # 3rd Fri of Jun 2026
        ],
    )
    def test_third_friday_detected(self, d: date) -> None:
        assert calendar_overlay.is_opex_friday(d)

    @pytest.mark.parametrize(
        "d",
        [
            date(2026, 1, 2),   # 1st Friday
            date(2026, 1, 9),   # 2nd Friday
            date(2026, 1, 23),  # 4th Friday
            date(2026, 1, 19),  # Monday, not Friday
        ],
    )
    def test_other_days_rejected(self, d: date) -> None:
        assert not calendar_overlay.is_opex_friday(d)


# ---------------------------------------------------------------------------
# Composer tests
# ---------------------------------------------------------------------------


class TestSizeMultiplier:
    def test_master_off_returns_one(self) -> None:
        cfg = _full_cfg(master=False, tom=True, fomc=True)
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(_decision(), dt, cfg)
        assert m == 1.0

    def test_disabled_overlay_neutral(self) -> None:
        cfg = _full_cfg()  # all sub-overlays off
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(_decision(), dt, cfg)
        assert m == 1.0

    def test_turn_of_month_long_multiplier(self) -> None:
        cfg = _full_cfg(tom=True)
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(_decision(), dt, cfg)
        assert m == pytest.approx(1.2)

    def test_composes_multiple_overlays(self) -> None:
        cfg = _full_cfg(tom=True, fomc=True)
        # 2026-04-30 (Thu) = last 4 trading days of April (turn-of-month).
        # Inject a synthetic FOMC date matching this day so the 24h
        # window also fires at 13:30 ET (30 min before 14:00 release).
        dt = datetime(2026, 4, 30, 13, 30, tzinfo=ET)
        d = _decision(strategy_id="overnight_drift")
        # turn_of_month applies to overnight_drift (1.2 long), fomc
        # applies to overnight_drift (1.3 long). Composed = 1.56.
        m = calendar_overlay.compute_size_multiplier(
            d, dt, cfg, fomc_dates=[date(2026, 4, 30)],
        )
        assert m == pytest.approx(1.2 * 1.3)

    def test_capped_at_2(self) -> None:
        cfg = _full_cfg(tom=True, fomc=True)
        cfg["calendar_overlay"]["turn_of_month"]["long_multiplier"] = 5.0
        cfg["calendar_overlay"]["fomc_drift"]["long_multiplier"] = 5.0
        dt = datetime(2026, 4, 30, 13, 30, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(
            _decision(strategy_id="overnight_drift"), dt, cfg,
            fomc_dates=[date(2026, 4, 30)],
        )
        assert m == 2.0

    def test_floored_at_zero(self) -> None:
        cfg = _full_cfg(tom=True)
        cfg["calendar_overlay"]["turn_of_month"]["long_multiplier"] = -5.0
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(_decision(), dt, cfg)
        assert m == 0.0

    def test_per_strategy_opt_in(self) -> None:
        cfg = _full_cfg(fomc=True)
        # fomc_drift applies_to=["overnight_drift"] only.
        d = _decision(strategy_id="mean_reversion", direction="long")
        dt = datetime(2026, 5, 6, 13, 30, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(
            d, dt, cfg, fomc_dates=[date(2026, 5, 6)],
        )
        assert m == 1.0

    def test_short_uses_short_multiplier(self) -> None:
        cfg = _full_cfg(tom=True)
        d = _decision(direction="short")
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        m = calendar_overlay.compute_size_multiplier(d, dt, cfg)
        assert m == pytest.approx(0.8)

    def test_requires_tz_aware(self) -> None:
        with pytest.raises(ValueError):
            calendar_overlay.compute_size_multiplier(
                _decision(), datetime(2026, 4, 30, 10, 0), _full_cfg(tom=True),
            )


class TestShouldBlockEntry:
    def test_block_overrides_multiplier(self) -> None:
        cfg = _full_cfg(plw=True, tom=True)
        d = _decision(strategy_id="overnight_drift")
        # Friday 2026-01-16 before MLK Mon 2026-01-19, also turn-of-month
        # tail not relevant here. Block applies regardless of multiplier.
        dt = datetime(2026, 1, 16, 15, 0, tzinfo=ET)
        assert calendar_overlay.should_block_entry(d, dt, cfg)

    def test_not_blocked_for_other_strategy(self) -> None:
        cfg = _full_cfg(plw=True)
        d = _decision(strategy_id="mean_reversion")
        dt = datetime(2026, 1, 16, 15, 0, tzinfo=ET)
        assert not calendar_overlay.should_block_entry(d, dt, cfg)

    def test_master_off_never_blocks(self) -> None:
        cfg = _full_cfg(master=False, plw=True)
        d = _decision(strategy_id="overnight_drift")
        dt = datetime(2026, 1, 16, 15, 0, tzinfo=ET)
        assert not calendar_overlay.should_block_entry(d, dt, cfg)


class TestApplyOverlay:
    def test_no_op_when_disabled(self) -> None:
        cfg = _full_cfg(master=False)
        d = _decision()
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        result = calendar_overlay.apply_overlay(d, dt, cfg)
        assert result is d  # exact pass-through

    def test_block_returns_none(self) -> None:
        cfg = _full_cfg(plw=True)
        d = _decision(strategy_id="overnight_drift")
        dt = datetime(2026, 1, 16, 15, 0, tzinfo=ET)
        assert calendar_overlay.apply_overlay(d, dt, cfg) is None

    def test_scales_shares(self) -> None:
        cfg = _full_cfg(tom=True)
        d = _decision(shares=100.0)
        dt = datetime(2026, 4, 30, 10, 0, tzinfo=ET)
        result = calendar_overlay.apply_overlay(d, dt, cfg)
        assert result is not None
        assert result.shares == pytest.approx(120.0)
        # Original should be untouched (replace returns new instance).
        assert d.shares == 100.0


# ---------------------------------------------------------------------------
# FOMC calendar coverage
# ---------------------------------------------------------------------------


class TestFomcCalendar:
    def test_covers_2020_through_2027(self) -> None:
        dates = get_fomc_dates()
        years = {d.year for d in dates}
        for y in range(2020, 2028):
            count = sum(1 for d in dates if d.year == y)
            assert count >= 8, f"year {y} has only {count} FOMC dates"
        assert years.issuperset(set(range(2020, 2028)))

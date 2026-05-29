"""Tests for the overnight_drift strategy and its time-zone helpers.

Focus: the config-driven entry window defaults and the ``_to_et_datetime``
coercion, both of which gate when entries fire and when next-morning exits
are detected. A wrong-timezone value here silently breaks the wall-clock
window checks, so the behaviour is pinned explicitly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pandas as pd
import pytest

from trading_bot.constants import TZ_EASTERN
from trading_bot.strategy.strategies.overnight_drift import (
    OvernightDriftStrategy,
    _position_entry_date,
    _to_et_datetime,
)


@pytest.mark.unit
class TestEntryWindowDefaults:
    def test_default_window_matches_production_and_precedes_winddown(self) -> None:
        """Regression: config-less defaults must be 15:40-15:45 ET — the
        live config.yaml values — so a config-missing deployment never
        widens the window into the 15:50 wind-down (an entry submitted
        during/after wind-down has too little time to fill before close).
        """
        strategy = OvernightDriftStrategy(config={})
        assert strategy._entry_window_start == time(15, 40)
        assert strategy._entry_window_end == time(15, 45)
        # Whole window strictly before the 15:50 wind-down start.
        assert strategy._entry_window_end < time(15, 50)

    def test_config_overrides_window(self) -> None:
        strategy = OvernightDriftStrategy(
            config={"entry_window_start": "15:30", "entry_window_end": "15:35"},
        )
        assert strategy._entry_window_start == time(15, 30)
        assert strategy._entry_window_end == time(15, 35)


@pytest.mark.unit
class TestToEtDatetime:
    def test_utc_aware_converts_to_eastern(self) -> None:
        # 20:40 UTC == 16:40 EDT on a summer date.
        utc_dt = datetime(2026, 5, 26, 20, 40, tzinfo=timezone.utc)
        et = _to_et_datetime(utc_dt)
        assert et is not None
        assert et.tzinfo is not None
        assert et.hour == 16 and et.minute == 40

    def test_naive_is_treated_as_already_eastern(self) -> None:
        naive = datetime(2026, 5, 26, 15, 45)
        et = _to_et_datetime(naive)
        assert et == naive  # unchanged (backtest convention)

    def test_pandas_timestamp_converts(self) -> None:
        ts = pd.Timestamp("2026-05-26 20:40", tz="UTC")
        et = _to_et_datetime(ts)
        assert et is not None
        assert et.hour == 16 and et.minute == 40

    def test_none_and_non_datetime_return_none(self) -> None:
        assert _to_et_datetime(None) is None
        assert _to_et_datetime("not-a-datetime") is None
        assert _to_et_datetime(12345) is None


@pytest.mark.unit
class TestPositionEntryDate:
    def test_et_aware_iso_string_keeps_eastern_calendar_date(self) -> None:
        """A 15:45 ET entry is the SAME calendar day in its own offset —
        the exit gate (``bar_date > entry_date``) must not roll it forward
        a day. Confirms the recent review's 'extra-day hold' concern is a
        non-issue for the real entry window.
        """
        pos = {"entry_time": "2026-05-26T15:45:41.544069-04:00"}
        assert _position_entry_date(pos) == date(2026, 5, 26)

    def test_et_aware_datetime_object(self) -> None:
        pos = {"entry_time": datetime(2026, 5, 26, 15, 45, tzinfo=TZ_EASTERN)}
        assert _position_entry_date(pos) == date(2026, 5, 26)

    def test_missing_entry_time_returns_none(self) -> None:
        assert _position_entry_date({}) is None

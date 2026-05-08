"""Property-based tests for critical math.

Hypothesis-driven invariants on pure functions in the live trading
path. Complements the existing example-based tests in:

  - `test_sizing_properties.py` (PositionSizer / size_by_risk)
  - `test_exit_logic.py` (check_time_stop dated cases)
  - `test_strategies.py` (RSI/SMA exact-value cases)

The defects in this codebase are mostly silent: wrong types floor to
zero, naive datetimes drift by a day, dates step over weekends without
counting them. Property tests turn "silent wrong" into "loud failure"
across thousands of generated inputs.

Targets selected for highest leverage:
  - `count_trading_days_between` — date arithmetic + holiday gaps;
    introduced in PR #83. Must agree with manual counting; must never
    return a negative or nonsense value.
  - `compute_sma` — pure math; output length, NaN-prefix length, and
    monotonicity properties are easy to assert.
  - `compute_rsi` — pure math; output bounded in [0, 100], NaN prefix
    length matches `period`.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.strategy.technical import TechnicalAnalyzer
# Import the production helper directly — single source of truth so
# the tested algorithm and the deployed one can't drift apart.
from trading_bot.utils.time import count_trading_days_between

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------------
# Date arithmetic — count_trading_days_between (utils.time)
# ---------------------------------------------------------------------
# Used by ExitManager.check_time_stop swing branch (PR #83).


# 2026-2027 dates only — within the HolidayCalendar's loaded range.
date_in_range = st.dates(
    min_value=date(2026, 1, 5),  # first Mon of 2026
    max_value=date(2027, 12, 24),
)


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(start=date_in_range, days_forward=st.integers(min_value=0, max_value=60))
def test_count_trading_days_never_negative(
    start: date, days_forward: int,
) -> None:
    """The count is non-negative for any valid (start, end) pair."""
    cal = HolidayCalendar()
    end = start + timedelta(days=days_forward)
    n = count_trading_days_between(cal, start, end)
    assert n >= 0, f"negative count={n} for {start} → {end}"


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(start=date_in_range, days_forward=st.integers(min_value=0, max_value=60))
def test_count_trading_days_bounded_by_calendar_distance(
    start: date, days_forward: int,
) -> None:
    """Trading days ≤ calendar days. A perfect run of weekdays without
    holidays gives equality; any weekend or holiday strictly reduces
    the count.
    """
    cal = HolidayCalendar()
    end = start + timedelta(days=days_forward)
    n = count_trading_days_between(cal, start, end)
    assert n <= days_forward, (
        f"count={n} exceeds calendar distance={days_forward} "
        f"for {start} → {end}"
    )


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(start=date_in_range, days_forward=st.integers(min_value=0, max_value=14))
def test_count_trading_days_matches_naive_loop(
    start: date, days_forward: int,
) -> None:
    """Exact agreement with a naive day-by-day count."""
    cal = HolidayCalendar()
    end = start + timedelta(days=days_forward)

    # Naive: walk start+1 .. end inclusive, count weekday-and-not-holiday
    expected = 0
    cursor = start + timedelta(days=1)
    while cursor <= end:
        if cursor.weekday() < 5 and not cal.is_holiday(cursor):
            expected += 1
        cursor += timedelta(days=1)

    actual = count_trading_days_between(cal, start, end)
    assert actual == expected, (
        f"mismatch for {start} → {end}: naive={expected}, helper={actual}"
    )


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(d=date_in_range, offset=st.integers(min_value=-30, max_value=0))
def test_count_trading_days_zero_when_end_le_start(
    d: date, offset: int,
) -> None:
    """``end_date <= start_date`` always returns 0 — even with a wildly
    backward delta, never negative or wraparound."""
    cal = HolidayCalendar()
    end = d + timedelta(days=offset)
    n = count_trading_days_between(cal, d, end)
    assert n == 0, f"got {n} for backward range {d} → {end}"


# ---------------------------------------------------------------------
# Compute_sma — closing-price moving average
# ---------------------------------------------------------------------

def _make_close_df(prices: list[float]) -> pd.DataFrame:
    """Wrap a price list as a single-column DataFrame for the analyzer."""
    return pd.DataFrame({"close": prices})


close_prices = st.lists(
    st.floats(min_value=1.0, max_value=10_000.0,
              allow_nan=False, allow_infinity=False),
    min_size=2, max_size=200,
)


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(prices=close_prices, period=st.integers(min_value=2, max_value=50))
def test_sma_output_length_matches_input(
    prices: list[float], period: int,
) -> None:
    """SMA series length matches input length, no silent truncation."""
    df = _make_close_df(prices)
    sma = TechnicalAnalyzer.compute_sma(df, period)
    assert len(sma) == len(prices)


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(prices=close_prices, period=st.integers(min_value=2, max_value=50))
def test_sma_nan_prefix_matches_period(
    prices: list[float], period: int,
) -> None:
    """The first ``period - 1`` values must be NaN (not enough history),
    and from index ``period - 1`` onward the SMA is defined whenever
    the input has enough rows."""
    df = _make_close_df(prices)
    sma = TechnicalAnalyzer.compute_sma(df, period)
    nan_count = sma.isna().sum()
    if len(prices) < period:
        # Whole series is NaN
        assert nan_count == len(prices)
    else:
        # Exactly ``period - 1`` leading NaNs
        assert nan_count == period - 1, (
            f"expected {period - 1} leading NaNs for period={period}, "
            f"got {nan_count}"
        )


@settings(max_examples=100, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(price=st.floats(min_value=1.0, max_value=10_000.0,
                       allow_nan=False, allow_infinity=False),
       n=st.integers(min_value=10, max_value=100),
       period=st.integers(min_value=2, max_value=10))
def test_sma_of_constant_series_is_that_constant(
    price: float, n: int, period: int,
) -> None:
    """For a flat price series, every defined SMA value equals the price."""
    df = _make_close_df([price] * n)
    sma = TechnicalAnalyzer.compute_sma(df, period)
    # After the warm-up, every value should equal `price`
    defined = sma.iloc[period - 1:]
    assert defined.notna().all()
    assert (defined - price).abs().max() < 1e-9


# ---------------------------------------------------------------------
# Compute_rsi — bounded oscillator
# ---------------------------------------------------------------------

@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(prices=st.lists(
    st.floats(min_value=1.0, max_value=10_000.0,
              allow_nan=False, allow_infinity=False),
    min_size=20, max_size=200,
), period=st.integers(min_value=5, max_value=30))
def test_rsi_bounded_in_0_100(
    prices: list[float], period: int,
) -> None:
    """RSI is mathematically bounded to [0, 100]. Any value outside is
    a calculation bug. NaN values are allowed (warm-up + zero-loss
    sentinels handled in the producer)."""
    df = _make_close_df(prices)
    rsi = TechnicalAnalyzer.compute_rsi(df, period=period)
    defined = rsi.dropna()
    if len(defined) == 0:
        return  # All-NaN is acceptable for very short series
    assert (defined >= 0.0).all(), f"RSI < 0: min={defined.min()}"
    assert (defined <= 100.0).all(), f"RSI > 100: max={defined.max()}"


@settings(max_examples=80, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(price=st.floats(min_value=1.0, max_value=10_000.0,
                       allow_nan=False, allow_infinity=False),
       n=st.integers(min_value=30, max_value=100),
       period=st.integers(min_value=5, max_value=14))
def test_rsi_of_constant_series_no_inf_or_nan_arithmetic(
    price: float, n: int, period: int,
) -> None:
    """For a flat price series, every delta is 0 → avg_loss is 0 → RS
    is mathematically undefined. The behavioral contract is "no
    division-by-zero leak": the output must not contain ``+inf`` or
    ``-inf``. Other valid implementations could legitimately return
    50.0 (mid), NaN, or simply skip the warm-up — all are acceptable
    so long as no infinities reach downstream code that compares
    against thresholds (`rsi < oversold`).

    The current implementation chose NaN-via-`replace(0, NaN)` (see
    ``compute_rsi``) because mid-point 50.0 would falsely satisfy
    most oversold-check predicates. If a future refactor switches
    strategies, this property still holds; only the
    ``test_rsi_bounded_in_0_100`` defined-values check needs to keep
    passing alongside.
    """
    import math
    df = _make_close_df([price] * n)
    rsi = TechnicalAnalyzer.compute_rsi(df, period=period)
    # Cast to float so isinf works element-wise.
    for v in rsi.dropna().tolist():
        assert not math.isinf(v), (
            f"flat series produced infinite RSI value {v!r} — "
            f"check zero-avg_loss handling in compute_rsi"
        )

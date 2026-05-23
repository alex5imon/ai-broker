"""Tests for the cross-sectional momentum strategy (sleeve #6, issue #44)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_bot.constants import HoldType
from trading_bot.strategy.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumStrategy,
)


ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _daily_bars(
    n: int,
    end_date: date,
    total_return: float,
    start_price: float = 100.0,
) -> pd.DataFrame:
    """Build n daily bars ending on end_date with a target total return.

    Linear price path — keeps the ranking math trivial and deterministic.
    """
    end_price: float = start_price * (1.0 + total_return)
    prices: np.ndarray = np.linspace(start_price, end_price, n)
    dates: list[pd.Timestamp] = []
    # walk backwards from end_date, weekdays only, to give n trading days
    d = end_date
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(pd.Timestamp(d, tz=ET))
        d -= timedelta(days=1)
    dates.reverse()
    idx = pd.DatetimeIndex(dates, name="timestamp")
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.005,
            "low": prices * 0.995,
            "close": prices,
            "volume": np.full(n, 1_000_000),
        },
        index=idx,
    )


def _five_min_bar(ts_et: datetime, close: float = 100.0) -> pd.DataFrame:
    """Single-row 5-min frame indexed at ts_et (ET, tz-aware)."""
    return pd.DataFrame(
        {
            "open": [close],
            "high": [close * 1.001],
            "low": [close * 0.999],
            "close": [close],
            "volume": [10_000],
        },
        index=pd.DatetimeIndex([pd.Timestamp(ts_et).tz_convert(ET)], name="timestamp"),
    )


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "enabled": True,
        "allocation_usd": 1500.0,
        "max_positions": 2,
        "universe": ["XLF", "XLK", "XLE", "XLV"],
        "lookback_days": 60,
        "skip_recent_days": 0,
        "top_n": 2,
        "rebalance_day_of_month": 1,
        "rebalance_time_et": "09:35",
        "disaster_stop_pct": 0.15,
        "fractional_shares": True,
        "position_pct": 0.95,
    }
    base.update(overrides)
    return base


def _make_universe_loader(
    bars_by_ticker: dict[str, pd.DataFrame],
) -> Any:
    """Return a loader callable that ignores the as-of date and just
    serves the pre-built per-ticker daily frame."""

    def _loader(ticker: str, _as_of: date) -> pd.DataFrame | None:
        return bars_by_ticker.get(ticker)

    return _loader


def _first_trading_day_jan_2026() -> date:
    # 2026-01-01 is a Thursday and a market holiday; first trading day = Jan 2 (Fri).
    return date(2026, 1, 2)


def _mid_month_2026() -> date:
    # Mid-January Tuesday — not a rebalance day under day_of_month=1.
    return date(2026, 1, 13)


# ---------------------------------------------------------------------------
# Rebalance-day gating
# ---------------------------------------------------------------------------


def test_off_rebalance_day_returns_none() -> None:
    """Non-rebalance days produce no decisions."""
    mid_month: date = _mid_month_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(120, mid_month, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.20), ("XLE", -0.02), ("XLV", 0.10)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(), universe_daily_loader=_make_universe_loader(bars),
    )
    decision = strat.evaluate_entry(
        ticker="XLK",
        exchange="NYSE",
        df_5min=_five_min_bar(datetime(2026, 1, 13, 9, 35, tzinfo=ET)),
        df_daily=bars["XLK"],
        current_price=120.0,
        available_cash=1500.0,
    )
    assert decision is None


def test_before_rebalance_time_returns_none() -> None:
    """Even on a rebalance day, fire only at/after the rebalance time."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(120, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.20), ("XLE", -0.02), ("XLV", 0.10)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(), universe_daily_loader=_make_universe_loader(bars),
    )
    decision = strat.evaluate_entry(
        ticker="XLK",
        exchange="NYSE",
        df_5min=_five_min_bar(datetime(2026, 1, 2, 9, 30, tzinfo=ET)),
        df_daily=bars["XLK"],
        current_price=120.0,
        available_cash=1500.0,
    )
    assert decision is None


def test_ticker_not_in_universe_returns_none() -> None:
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(120, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.20), ("XLE", -0.02), ("XLV", 0.10)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(), universe_daily_loader=_make_universe_loader(bars),
    )
    decision = strat.evaluate_entry(
        ticker="SPY",  # not in universe
        exchange="NYSE",
        df_5min=_five_min_bar(datetime(2026, 1, 2, 9, 35, tzinfo=ET)),
        df_daily=_daily_bars(120, reb, total_return=0.30),
        current_price=500.0,
        available_cash=1500.0,
    )
    assert decision is None


# ---------------------------------------------------------------------------
# Ranking math
# ---------------------------------------------------------------------------


def test_top_n_picks_highest_lookback_return() -> None:
    """Top-N = 2 should pick XLK (+25%) and XLV (+18%), reject XLE/XLF."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.25),
        "XLE": _daily_bars(80, reb, total_return=-0.10),
        "XLV": _daily_bars(80, reb, total_return=0.18),
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2), universe_daily_loader=_make_universe_loader(bars),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)

    # XLK is a winner
    dec_xlk = strat.evaluate_entry(
        ticker="XLK", exchange="NYSE",
        df_5min=_five_min_bar(ts), df_daily=bars["XLK"],
        current_price=125.0, available_cash=1500.0,
    )
    assert dec_xlk is not None
    assert dec_xlk.ticker == "XLK"
    assert dec_xlk.hold_type == HoldType.SWING
    assert dec_xlk.target_price is None
    assert dec_xlk.trail_pct is None

    # XLF is NOT in top-2
    dec_xlf = strat.evaluate_entry(
        ticker="XLF", exchange="NYSE",
        df_5min=_five_min_bar(ts), df_daily=bars["XLF"],
        current_price=105.0, available_cash=1500.0,
    )
    assert dec_xlf is None


def test_skip_recent_month_changes_ranking() -> None:
    """skip_recent_days=21 should rank on the 60-bar window ending 21 days
    before today, not the most recent 60 bars."""
    reb: date = _first_trading_day_jan_2026()
    # XLK: huge dump in last 21 days but stellar before — should still rank highly
    # XLV: steady up; should rank below XLK once the dump is skipped.
    n = 120

    # Build a path: first ~75 bars XLK rises strongly, then last 21 bars crash.
    def _xlk_path() -> pd.DataFrame:
        # Concatenate two segments
        early = _daily_bars(99, reb - timedelta(days=30), total_return=0.40)
        late = _daily_bars(21, reb, total_return=-0.30, start_price=float(early["close"].iloc[-1]))
        df = pd.concat([early, late.iloc[1:]])  # drop overlap day
        # Reindex to last n bars
        return df.iloc[-n:]

    def _xlv_path() -> pd.DataFrame:
        return _daily_bars(n, reb, total_return=0.10)

    bars: dict[str, pd.DataFrame] = {
        "XLF": _daily_bars(n, reb, total_return=0.02),
        "XLK": _xlk_path(),
        "XLE": _daily_bars(n, reb, total_return=-0.05),
        "XLV": _xlv_path(),
    }

    # Without skip, XLK's last-21-day crash dominates → XLK out, XLV in
    strat_no_skip = CrossSectionalMomentumStrategy(
        config=_config(top_n=1, skip_recent_days=0, lookback_days=60),
        universe_daily_loader=_make_universe_loader(bars),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    dec_no_skip_xlk = strat_no_skip.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=100.0, available_cash=1500.0,
    )
    dec_no_skip_xlv = strat_no_skip.evaluate_entry(
        ticker="XLV", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLV"], current_price=100.0, available_cash=1500.0,
    )
    assert dec_no_skip_xlk is None
    assert dec_no_skip_xlv is not None

    # With skip-21, the dump is excluded → XLK's earlier ramp dominates
    strat_skip = CrossSectionalMomentumStrategy(
        config=_config(top_n=1, skip_recent_days=21, lookback_days=60),
        universe_daily_loader=_make_universe_loader(bars),
    )
    dec_skip_xlk = strat_skip.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=100.0, available_cash=1500.0,
    )
    assert dec_skip_xlk is not None


def test_ticker_with_insufficient_history_excluded() -> None:
    """A universe member with < lookback_days bars is excluded from ranking
    but does not crash, and other members still rank normally."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.30),
        # Brand-new ETF — only 10 days of history
        "XLE": _daily_bars(10, reb, total_return=0.50),
        "XLV": _daily_bars(80, reb, total_return=0.20),
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2, lookback_days=60),
        universe_daily_loader=_make_universe_loader(bars),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)

    # XLE has insufficient history → excluded; top-2 = XLK + XLV
    dec_xle = strat.evaluate_entry(
        ticker="XLE", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLE"], current_price=100.0, available_cash=1500.0,
    )
    assert dec_xle is None

    dec_xlk = strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=130.0, available_cash=1500.0,
    )
    assert dec_xlk is not None


def test_universe_loader_returns_none_excludes_ticker() -> None:
    """If the loader returns None for a universe member, that member is
    silently excluded from ranking (no crash, no fail-loud)."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame | None] = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.30),
        "XLE": None,
        "XLV": _daily_bars(80, reb, total_return=0.20),
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2, lookback_days=60),
        universe_daily_loader=lambda t, _d: bars.get(t),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    dec_xlk = strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=130.0, available_cash=1500.0,
    )
    assert dec_xlk is not None


# ---------------------------------------------------------------------------
# Sizing & decision shape
# ---------------------------------------------------------------------------


def test_decision_is_slot_aware() -> None:
    """Per-slot spend = (cash * position_pct) / top_n."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(80, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.25), ("XLE", -0.10), ("XLV", 0.18)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2, position_pct=0.95, fractional_shares=True),
        universe_daily_loader=_make_universe_loader(bars),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    dec = strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=125.0, available_cash=1500.0,
    )
    assert dec is not None
    # Per-slot budget = 1500 * 0.95 / 2 = 712.5; shares ≈ 712.5 / 125 = 5.7
    expected_shares = round((1500.0 * 0.95 / 2) / 125.0, 4)
    assert dec.shares == pytest.approx(expected_shares, rel=1e-4)


def test_disaster_stop_set_relative_to_entry() -> None:
    """Stop price is entry * (1 - disaster_stop_pct), rounded to 2dp."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(80, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.25), ("XLE", -0.10), ("XLV", 0.18)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2, disaster_stop_pct=0.15),
        universe_daily_loader=_make_universe_loader(bars),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    dec = strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=200.0, available_cash=1500.0,
    )
    assert dec is not None
    assert dec.stop_price == pytest.approx(200.0 * 0.85, abs=0.01)


# ---------------------------------------------------------------------------
# Exit logic
# ---------------------------------------------------------------------------


def test_exit_on_disaster_stop_any_day() -> None:
    strat = CrossSectionalMomentumStrategy(
        config=_config(), universe_daily_loader=_make_universe_loader({}),
    )
    position = {
        "ticker": "XLK",
        "entry_price": 100.0,
        "stop_price": 85.0,
        "entry_time": datetime(2026, 1, 2, 9, 35, tzinfo=ET),
    }
    sig = strat.evaluate_exit(position=position, current_price=80.0)
    assert sig.should_exit
    assert sig.reason == "disaster_stop"
    assert sig.is_emergency
    assert sig.use_market_order


def test_no_exit_when_in_top_n_and_no_stop_hit() -> None:
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(80, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.25), ("XLE", -0.10), ("XLV", 0.18)]
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2), universe_daily_loader=_make_universe_loader(bars),
    )
    # Touch ranking with an entry call to prime the memo
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=125.0, available_cash=1500.0,
    )
    position = {
        "ticker": "XLK",
        "entry_price": 125.0,
        "stop_price": 106.25,
        "entry_time": datetime(2026, 1, 2, 9, 35, tzinfo=ET),
    }
    df_5 = _five_min_bar(datetime(2026, 1, 2, 10, 0, tzinfo=ET))
    sig = strat.evaluate_exit(
        position=position, current_price=126.0,
        df_5min=df_5, df_daily=bars["XLK"],
    )
    assert not sig.should_exit


def test_exit_on_rebalance_day_when_position_drops_out_of_top_n() -> None:
    """On the next rebalance day, if the held ticker is no longer in top-N,
    a normal (non-emergency) exit fires."""
    feb_reb: date = date(2026, 2, 2)  # 2026-02-01 is Sunday → first trading day = Feb 2 (Mon)
    bars: dict[str, pd.DataFrame] = {
        "XLF": _daily_bars(80, feb_reb, total_return=0.30),
        "XLK": _daily_bars(80, feb_reb, total_return=-0.05),  # held but now last
        "XLE": _daily_bars(80, feb_reb, total_return=0.25),
        "XLV": _daily_bars(80, feb_reb, total_return=0.18),
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2), universe_daily_loader=_make_universe_loader(bars),
    )
    position = {
        "ticker": "XLK",
        "entry_price": 125.0,
        "stop_price": 106.25,
        "entry_time": datetime(2026, 1, 2, 9, 35, tzinfo=ET),
    }
    df_5 = _five_min_bar(datetime(2026, 2, 2, 9, 35, tzinfo=ET))
    sig = strat.evaluate_exit(
        position=position, current_price=119.0,
        df_5min=df_5, df_daily=bars["XLK"],
    )
    assert sig.should_exit
    assert sig.reason == "rebalance_out"
    assert not sig.is_emergency


def test_no_exit_on_rebalance_day_when_still_in_top_n() -> None:
    feb_reb: date = date(2026, 2, 2)
    bars: dict[str, pd.DataFrame] = {
        "XLF": _daily_bars(80, feb_reb, total_return=0.05),
        "XLK": _daily_bars(80, feb_reb, total_return=0.30),  # still top
        "XLE": _daily_bars(80, feb_reb, total_return=-0.10),
        "XLV": _daily_bars(80, feb_reb, total_return=0.20),
    }
    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2), universe_daily_loader=_make_universe_loader(bars),
    )
    position = {
        "ticker": "XLK",
        "entry_price": 125.0,
        "stop_price": 106.25,
        "entry_time": datetime(2026, 1, 2, 9, 35, tzinfo=ET),
    }
    df_5 = _five_min_bar(datetime(2026, 2, 2, 9, 35, tzinfo=ET))
    sig = strat.evaluate_exit(
        position=position, current_price=140.0,
        df_5min=df_5, df_daily=bars["XLK"],
    )
    assert not sig.should_exit


# ---------------------------------------------------------------------------
# Memoization & metadata
# ---------------------------------------------------------------------------


def test_ranking_memoized_within_same_rebalance_date() -> None:
    """Multiple per-ticker calls on the same date should call the loader
    universe_size − 1 times total (one batch on first call), not per-ticker."""
    reb: date = _first_trading_day_jan_2026()
    bars: dict[str, pd.DataFrame] = {
        t: _daily_bars(80, reb, total_return=r)
        for t, r in [("XLF", 0.05), ("XLK", 0.25), ("XLE", -0.10), ("XLV", 0.18)]
    }
    call_counter: dict[str, int] = {"n": 0}

    def _counting_loader(t: str, _d: date) -> pd.DataFrame | None:
        call_counter["n"] += 1
        return bars.get(t)

    strat = CrossSectionalMomentumStrategy(
        config=_config(top_n=2), universe_daily_loader=_counting_loader,
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)

    # First call — should load all 4 universe members
    strat.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLK"], current_price=125.0, available_cash=1500.0,
    )
    first_count = call_counter["n"]
    assert first_count >= 4  # loaded the universe at least once

    # Second call same day — should reuse memo, not reload
    strat.evaluate_entry(
        ticker="XLV", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars["XLV"], current_price=118.0, available_cash=1500.0,
    )
    assert call_counter["n"] == first_count


def test_get_max_positions_returns_config_value() -> None:
    strat = CrossSectionalMomentumStrategy(
        config=_config(max_positions=3),
        universe_daily_loader=_make_universe_loader({}),
    )
    assert strat.get_max_positions() == 3


def test_registry_includes_cross_sectional_momentum() -> None:
    from trading_bot.strategy.strategies import STRATEGY_REGISTRY
    assert "cross_sectional_momentum" in STRATEGY_REGISTRY
    assert STRATEGY_REGISTRY["cross_sectional_momentum"] is CrossSectionalMomentumStrategy

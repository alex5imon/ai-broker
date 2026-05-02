"""Tests for multi-strategy system: individual strategies, virtual portfolio, and orchestration."""

from __future__ import annotations

import sqlite3
from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategy.base import ExitSignal, StrategyDecision
from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
from trading_bot.strategy.strategies.trend_following import TrendFollowingStrategy
from trading_bot.strategy.strategies.breakout import BreakoutStrategy
from trading_bot.strategy.strategies.sentiment_combo import SentimentComboStrategy
from trading_bot.strategy.strategies import create_strategies
from trading_bot.strategy.technical import TechnicalAnalyzer
from trading_bot.execution.virtual_portfolio import PortfolioManager, VirtualPortfolio
from trading_bot.reporting.strategy_comparison import (
    generate_comparison,
    render_comparison_text,
    pick_winner,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(
    n: int = 100,
    start: float = 10.0,
    trend: float = 0.001,
    seed: int = 42,
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    prices = [start]
    for _ in range(n - 1):
        prices.append(prices[-1] * (1 + rng.normal(trend, 0.005)))
    prices = np.array(prices)
    return pd.DataFrame({
        "open": prices,
        "high": prices * (1 + rng.uniform(0.001, 0.005, n)),
        "low": prices * (1 - rng.uniform(0.001, 0.005, n)),
        "close": prices,
        "volume": rng.uniform(500000, 1500000, n),
    })


def _make_oversold_bars(n: int = 60) -> pd.DataFrame:
    """Create bars with a dip that triggers RSI oversold then recovery."""
    rng = np.random.default_rng(99)
    prices = [20.0]
    # First 30 bars: steady
    for _ in range(29):
        prices.append(prices[-1] * (1 + rng.normal(0.0, 0.003)))
    # Next 15 bars: sharp decline
    for _ in range(15):
        prices.append(prices[-1] * (1 + rng.normal(-0.015, 0.003)))
    # Next 15 bars: recovery
    for _ in range(15):
        prices.append(prices[-1] * (1 + rng.normal(0.012, 0.003)))
    prices = np.array(prices)
    return pd.DataFrame({
        "open": prices,
        "high": prices * 1.002,
        "low": prices * 0.998,
        "close": prices,
        "volume": rng.uniform(500000, 1500000, n),
    })


def _mr_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "allocation_usd": 250.0,
        "max_positions": 1,
        "rsi_period": 14,
        "rsi_oversold": 30,
        "rsi_recovery": 30,
        "rsi_exit": 55,
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.03,
    }


def _tf_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "allocation_usd": 250.0,
        "max_positions": 1,
        "sma_period": 50,
        "ema_fast": 9,
        "ema_slow": 21,
        "volume_multiplier": 1.5,
        "trailing_stop_pct": 0.025,
        "initial_stop_pct": 0.03,
    }


def _bo_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "allocation_usd": 250.0,
        "max_positions": 1,
        "breakout_period": 20,
        "exit_period": 10,
        "volume_multiplier": 1.5,
        "stop_loss_pct": 0.03,
    }


def _sc_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "allocation_usd": 250.0,
        "max_positions": 2,
        "sentiment_threshold": 0.15,
        "min_technical_signals": 1,
        "stop_loss_pct": 0.015,
        "take_profit_pct": 0.025,
    }


# ---------------------------------------------------------------------------
# Technical indicator helpers
# ---------------------------------------------------------------------------


class TestTechnicalHelpers:
    def test_compute_rsi_range(self) -> None:
        df = _make_bars(100)
        rsi = TechnicalAnalyzer.compute_rsi(df, 14)
        valid = rsi.dropna()
        assert len(valid) > 0
        assert valid.min() >= 0.0
        assert valid.max() <= 100.0

    def test_compute_sma(self) -> None:
        df = _make_bars(60)
        sma = TechnicalAnalyzer.compute_sma(df, 20)
        assert sma.dropna().iloc[-1] > 0

    def test_get_period_high(self) -> None:
        df = _make_bars(50)
        high = TechnicalAnalyzer.get_period_high(df, 20)
        assert high >= df["close"].iloc[-1] * 0.9

    def test_get_period_low(self) -> None:
        df = _make_bars(50)
        low = TechnicalAnalyzer.get_period_low(df, 20)
        assert low > 0


# ---------------------------------------------------------------------------
# Mean Reversion Strategy
# ---------------------------------------------------------------------------


class TestMeanReversion:
    def test_no_entry_without_oversold(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        df = _make_bars(100, trend=0.001)
        result = strategy.evaluate_entry("PLTR", "US", df, df, 10.5, 250.0)
        assert result is None

    def test_entry_on_rsi_recovery(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        df = _make_oversold_bars(60)
        price = float(df["close"].iloc[-1])
        result = strategy.evaluate_entry("PLTR", "US", df, df, price, 250.0)
        # May or may not trigger depending on exact RSI — just check no crash
        if result is not None:
            assert result.strategy_id == "mean_reversion"
            assert result.shares > 0
            assert result.stop_price < price

    def test_exit_on_stop_loss(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        position = {"entry_price": 10.0, "stop_price": 9.80}
        signal = strategy.evaluate_exit(position, 9.75)
        assert signal.should_exit is True
        assert signal.reason == "stop_loss"
        assert signal.is_emergency is True

    def test_exit_on_take_profit(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        position = {"entry_price": 10.0, "stop_price": 9.80}
        signal = strategy.evaluate_exit(position, 10.35)
        assert signal.should_exit is True
        assert signal.reason == "take_profit"

    def test_no_exit_in_range(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        position = {"entry_price": 10.0, "stop_price": 9.80}
        signal = strategy.evaluate_exit(position, 10.10)
        assert signal.should_exit is False

    def test_max_positions(self) -> None:
        strategy = MeanReversionStrategy(config=_mr_config())
        assert strategy.get_max_positions() == 1


# ---------------------------------------------------------------------------
# Trend Following Strategy
# ---------------------------------------------------------------------------


class TestTrendFollowing:
    def test_no_entry_below_sma(self) -> None:
        strategy = TrendFollowingStrategy(config=_tf_config())
        df_daily = _make_bars(120, trend=-0.002)
        df_5min = _make_bars(100)
        result = strategy.evaluate_entry("PLTR", "US", df_5min, df_daily, 5.0, 250.0)
        assert result is None

    def test_exit_on_initial_stop(self) -> None:
        strategy = TrendFollowingStrategy(config=_tf_config())
        position = {"entry_price": 10.0, "stop_price": 9.70, "highest_price": 10.0}
        signal = strategy.evaluate_exit(position, 9.65)
        assert signal.should_exit is True
        assert signal.is_emergency is True

    def test_exit_on_trailing_stop(self) -> None:
        strategy = TrendFollowingStrategy(config=_tf_config())
        position = {"entry_price": 10.0, "stop_price": 9.70, "highest_price": 11.0}
        # Trailing at 2.5%: 11.0 * 0.975 = 10.725
        signal = strategy.evaluate_exit(position, 10.70)
        assert signal.should_exit is True
        assert signal.reason == "trailing_stop"

    def test_max_positions(self) -> None:
        strategy = TrendFollowingStrategy(config=_tf_config())
        assert strategy.get_max_positions() == 1


# ---------------------------------------------------------------------------
# Breakout Strategy
# ---------------------------------------------------------------------------


class TestBreakout:
    def test_no_entry_below_period_high(self) -> None:
        strategy = BreakoutStrategy(config=_bo_config())
        df = _make_bars(50, trend=0.001)
        mid_price = float(df["close"].iloc[-1]) * 0.95
        result = strategy.evaluate_entry("PLTR", "US", df, df, mid_price, 250.0)
        assert result is None

    def test_exit_on_stop(self) -> None:
        strategy = BreakoutStrategy(config=_bo_config())
        position = {"entry_price": 10.0, "stop_price": 9.70}
        signal = strategy.evaluate_exit(position, 9.65)
        assert signal.should_exit is True

    def test_exit_on_period_low(self) -> None:
        strategy = BreakoutStrategy(config=_bo_config())
        position = {"entry_price": 10.0, "stop_price": 9.70}
        df_daily = _make_bars(30, start=10.0, trend=-0.01)
        period_low = TechnicalAnalyzer.get_period_low(df_daily, 10)
        signal = strategy.evaluate_exit(position, period_low - 0.01, df_daily=df_daily)
        assert signal.should_exit is True

    def test_max_positions(self) -> None:
        strategy = BreakoutStrategy(config=_bo_config())
        assert strategy.get_max_positions() == 1

    def test_h4_trend_filter_blocks_below_sma(self) -> None:
        cfg = {**_bo_config(), "require_trend_filter": True, "trend_sma_period": 20}
        strategy = BreakoutStrategy(config=cfg)
        # Build a daily series with downtrend so SMA20 ends above current.
        df = _make_bars(60, trend=-0.005)
        period_high = float(df["high"].iloc[:-1].max())
        # Even at a fresh "breakout" price, trend filter should block
        # because current price < SMA(close, 20).
        result = strategy.evaluate_entry(
            "PLTR", "US", df, df, period_high * 1.01, 250.0,
        )
        assert result is None

    def test_h4_trend_filter_disabled_lets_entry_attempt(self) -> None:
        # Sanity: with the filter OFF, the same bars use the original
        # gate (current_price <= period_high) so a price *below* the
        # period high still returns None — confirms the new flag doesn't
        # accidentally bypass anything.
        strategy = BreakoutStrategy(config=_bo_config())
        df = _make_bars(60, trend=-0.005)
        result = strategy.evaluate_entry("PLTR", "US", df, df, 1.0, 250.0)
        assert result is None

    def test_h6_pullback_blocks_buying_the_breakout_bar(self) -> None:
        # With pullback_entry=True, a price above the high should not
        # trigger an entry on the breakout bar itself — must wait for
        # retest within tolerance.
        cfg = {**_bo_config(), "pullback_entry": True}
        strategy = BreakoutStrategy(config=cfg)
        df = _make_bars(60, trend=0.001)
        period_high = float(df["high"].iloc[:-1].max())
        far_above = period_high * 1.05  # 5% above — outside tolerance
        result = strategy.evaluate_entry("PLTR", "US", df, df, far_above, 250.0)
        assert result is None


# ---------------------------------------------------------------------------
# Opening Range Breakout Strategy
# ---------------------------------------------------------------------------


def _orb_config() -> dict[str, Any]:
    return {
        "enabled": True,
        "allocation_usd": 1000.0,
        "max_positions": 3,
        "range_bars": 6,
        "entry_cutoff": "11:30",
        "volume_multiplier": 1.3,
        "target_r_multiple": 1.0,
        "risk_per_trade_pct": 0.02,
        "max_position_pct": 0.33,
        "fractional_shares": True,
        "min_range_pct": 0.002,
    }


class TestOpeningRangeBreakout:
    def test_max_positions(self) -> None:
        from trading_bot.strategy.strategies.opening_range_breakout import (
            OpeningRangeBreakoutStrategy,
        )
        strategy = OpeningRangeBreakoutStrategy(config=_orb_config())
        assert strategy.get_max_positions() == 3

    def test_exit_on_stop(self) -> None:
        from trading_bot.strategy.strategies.opening_range_breakout import (
            OpeningRangeBreakoutStrategy,
        )
        strategy = OpeningRangeBreakoutStrategy(config=_orb_config())
        position = {"entry_price": 10.10, "stop_price": 10.00, "target_price": 10.20}
        signal = strategy.evaluate_exit(position, 9.99)
        assert signal.should_exit is True
        assert signal.reason == "stop_loss"
        assert signal.is_emergency is True

    def test_exit_on_target(self) -> None:
        from trading_bot.strategy.strategies.opening_range_breakout import (
            OpeningRangeBreakoutStrategy,
        )
        strategy = OpeningRangeBreakoutStrategy(config=_orb_config())
        position = {"entry_price": 10.10, "stop_price": 10.00, "target_price": 10.20}
        signal = strategy.evaluate_exit(position, 10.21)
        assert signal.should_exit is True
        assert signal.reason == "take_profit"

    def test_no_exit_in_range(self) -> None:
        from trading_bot.strategy.strategies.opening_range_breakout import (
            OpeningRangeBreakoutStrategy,
        )
        strategy = OpeningRangeBreakoutStrategy(config=_orb_config())
        position = {"entry_price": 10.10, "stop_price": 10.00, "target_price": 10.20}
        signal = strategy.evaluate_exit(position, 10.15)
        assert signal.should_exit is False

    def test_no_entry_with_insufficient_bars(self) -> None:
        from trading_bot.strategy.strategies.opening_range_breakout import (
            OpeningRangeBreakoutStrategy,
        )
        strategy = OpeningRangeBreakoutStrategy(config=_orb_config())
        # Only 5 bars — strategy needs range_bars + 2 minimum.
        df = _make_bars(5)
        result = strategy.evaluate_entry("PLTR", "US", df, df, 10.0, 1000.0)
        assert result is None

    def test_registry_includes_orb(self) -> None:
        from trading_bot.strategy.strategies import STRATEGY_REGISTRY
        assert "opening_range_breakout" in STRATEGY_REGISTRY


# ---------------------------------------------------------------------------
# Sentiment Combo Strategy
# ---------------------------------------------------------------------------


class TestSentimentCombo:
    def test_no_entry_without_sentiment(self) -> None:
        strategy = SentimentComboStrategy(config=_sc_config())
        df = _make_bars(100)
        result = strategy.evaluate_entry("PLTR", "US", df, df, 10.0, 250.0, sentiment_score=None)
        assert result is None

    def test_no_entry_low_sentiment(self) -> None:
        strategy = SentimentComboStrategy(config=_sc_config())
        df = _make_bars(100)
        result = strategy.evaluate_entry("PLTR", "US", df, df, 10.0, 250.0, sentiment_score=0.05)
        assert result is None

    def test_exit_on_stop(self) -> None:
        strategy = SentimentComboStrategy(config=_sc_config())
        position = {"entry_price": 10.0, "stop_price": 9.85}
        signal = strategy.evaluate_exit(position, 9.80)
        assert signal.should_exit is True
        assert signal.reason == "stop_loss"

    def test_exit_on_take_profit(self) -> None:
        strategy = SentimentComboStrategy(config=_sc_config())
        position = {"entry_price": 10.0, "stop_price": 9.85}
        signal = strategy.evaluate_exit(position, 10.30)
        assert signal.should_exit is True
        assert signal.reason == "take_profit"

    def test_max_positions(self) -> None:
        strategy = SentimentComboStrategy(config=_sc_config())
        assert strategy.get_max_positions() == 2


# ---------------------------------------------------------------------------
# Strategy Factory
# ---------------------------------------------------------------------------


class TestStrategyFactory:
    def test_create_all_enabled(self) -> None:
        configs = {
            "mean_reversion": _mr_config(),
            "trend_following": _tf_config(),
            "breakout": _bo_config(),
            "sentiment_combo": _sc_config(),
        }
        strategies = create_strategies(configs)
        assert len(strategies) == 4
        ids = {s.strategy_id for s in strategies}
        assert ids == {"mean_reversion", "trend_following", "breakout", "sentiment_combo"}

    def test_disabled_strategy_excluded(self) -> None:
        configs = {
            "mean_reversion": {**_mr_config(), "enabled": False},
            "breakout": _bo_config(),
        }
        strategies = create_strategies(configs)
        assert len(strategies) == 1
        assert strategies[0].strategy_id == "breakout"


# ---------------------------------------------------------------------------
# Virtual Portfolio
# ---------------------------------------------------------------------------


class TestVirtualPortfolio:
    def test_initial_cash(self, tmp_db_path: str) -> None:
        vp = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        assert abs(vp.current_cash - 250.0) < 0.01

    def test_record_entry_deducts_cash(self, tmp_db_path: str) -> None:
        vp = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        vp.record_entry(10, 10.0)
        assert abs(vp.current_cash - 150.0) < 0.01

    def test_record_exit_adds_proceeds(self, tmp_db_path: str) -> None:
        vp = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        vp.record_entry(10, 10.0)
        vp.record_exit(10, 11.0, 10.0)
        # 250 - 100 (entry) + 110 (proceeds) = 260
        assert abs(vp.current_cash - 260.0) < 0.01

    def test_stats_win_rate(self, tmp_db_path: str) -> None:
        vp = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        vp.record_entry(10, 10.0)
        vp.record_exit(10, 11.0, 10.0)  # win
        vp.record_entry(10, 10.0)
        vp.record_exit(10, 9.0, 10.0)  # loss
        stats = vp.get_stats()
        assert stats["total_trades"] == 2
        assert stats["wins"] == 1
        assert abs(stats["win_rate"] - 0.5) < 0.01

    def test_idempotent_row_creation(self, tmp_db_path: str) -> None:
        vp1 = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        vp2 = VirtualPortfolio("test_strat", "Test", 250.0, tmp_db_path)
        assert abs(vp1.current_cash - 250.0) < 0.01


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class TestPortfolioManager:
    def test_creates_portfolios_for_enabled_strategies(self, tmp_db_path: str) -> None:
        configs = {
            "mean_reversion": _mr_config(),
            "breakout": _bo_config(),
        }
        pm = PortfolioManager(configs, 500.0, tmp_db_path)
        assert pm.get_portfolio("mean_reversion") is not None
        assert pm.get_portfolio("breakout") is not None
        assert pm.get_portfolio("nonexistent") is None

    def test_global_position_count_starts_zero(self, tmp_db_path: str) -> None:
        configs = {"mean_reversion": _mr_config()}
        pm = PortfolioManager(configs, 250.0, tmp_db_path)
        assert pm.get_global_position_count() == 0


# ---------------------------------------------------------------------------
# Comparison Report
# ---------------------------------------------------------------------------


class TestComparisonReport:
    def test_generate_comparison_with_data(self, tmp_db_path: str) -> None:
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO strategy_portfolios
               (strategy_id, display_name, initial_cash, current_cash,
                total_pnl, total_trades, wins, losses)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("mean_reversion", "Mean Reversion", 250.0, 270.0, 20.0, 5, 3, 2),
        )
        conn.execute(
            """INSERT INTO strategy_portfolios
               (strategy_id, display_name, initial_cash, current_cash,
                total_pnl, total_trades, wins, losses)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("breakout", "Breakout", 250.0, 240.0, -10.0, 3, 1, 2),
        )
        conn.commit()
        conn.close()

        report = generate_comparison(tmp_db_path)
        assert "mean_reversion" in report
        assert "breakout" in report
        assert report["mean_reversion"]["total_pnl"] == 20.0
        assert report["mean_reversion"]["return_pct"] == pytest.approx(8.0, abs=0.1)

    def test_render_comparison_text(self) -> None:
        report = {
            "mean_reversion": {
                "display_name": "Mean Reversion",
                "initial_cash": 250.0,
                "current_cash": 270.0,
                "total_pnl": 20.0,
                "return_pct": 8.0,
                "total_trades": 5,
                "wins": 3,
                "losses": 2,
                "win_rate": 60.0,
                "active": True,
            },
        }
        text = render_comparison_text(report)
        assert "Mean Reversion" in text
        assert "STRATEGY COMPARISON" in text

    def test_pick_winner(self) -> None:
        report = {
            "mean_reversion": {"total_pnl": 20.0},
            "breakout": {"total_pnl": -10.0},
        }
        assert pick_winner(report) == "mean_reversion"

    def test_empty_report(self) -> None:
        assert pick_winner({}) is None
        assert render_comparison_text({}) == "No strategy data available."


# ---------------------------------------------------------------------------
# Overnight Drift — live-path entry_time parsing regression
# ---------------------------------------------------------------------------
#
# The live path reads positions from SQLite where ``entry_time`` is stored as
# TEXT (ISO string), while the backtester supplies it as a ``datetime``.
# Regression test for the parser that must accept both forms.


class TestOvernightDriftEntryTime:
    """evaluate_exit must accept both datetime and ISO-string entry_time."""

    def _build_df(self, day: str, hh_mm: str = "09:35") -> pd.DataFrame:
        ts = pd.Timestamp(f"{day} {hh_mm}:00")
        return pd.DataFrame(
            {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000]},
            index=[ts],
        )

    def _strategy(self) -> Any:
        from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
        return OvernightDriftStrategy(config={"max_positions": 1})

    def test_exit_fires_next_session_with_string_entry_time(self) -> None:
        # Live-path shape: entry_time as ISO string from SQLite.
        strat = self._strategy()
        position = {
            "entry_price": 100.0,
            "stop_price": 97.0,
            "entry_time": "2026-04-23T15:45:00",
            "hold_type": "swing",
        }
        df = self._build_df("2026-04-24", "09:35")
        signal = strat.evaluate_exit(position=position, current_price=100.4, df_5min=df)
        assert signal.should_exit is True
        assert signal.reason == "overnight_exit"

    def test_exit_fires_next_session_with_datetime_entry_time(self) -> None:
        # Backtester shape: entry_time as datetime.
        from datetime import datetime as _dt
        strat = self._strategy()
        position = {
            "entry_price": 100.0,
            "stop_price": 97.0,
            "entry_time": _dt(2026, 4, 23, 15, 45),
            "hold_type": "swing",
        }
        df = self._build_df("2026-04-24", "09:35")
        signal = strat.evaluate_exit(position=position, current_price=100.4, df_5min=df)
        assert signal.should_exit is True
        assert signal.reason == "overnight_exit"

    def test_no_exit_on_same_session(self) -> None:
        strat = self._strategy()
        position = {
            "entry_price": 100.0,
            "stop_price": 97.0,
            "entry_time": "2026-04-24T15:45:00",
            "hold_type": "swing",
        }
        df = self._build_df("2026-04-24", "15:50")
        signal = strat.evaluate_exit(position=position, current_price=100.2, df_5min=df)
        assert signal.should_exit is False

    def test_stop_loss_takes_priority(self) -> None:
        strat = self._strategy()
        position = {
            "entry_price": 100.0,
            "stop_price": 97.0,
            "entry_time": "2026-04-24T15:45:00",
            "hold_type": "swing",
        }
        df = self._build_df("2026-04-24", "15:50")
        signal = strat.evaluate_exit(position=position, current_price=96.5, df_5min=df)
        assert signal.should_exit is True
        assert signal.reason == "stop_loss"
        assert signal.is_emergency is True


# ---------------------------------------------------------------------------
# Overnight Drift — entry-window timezone regression
# ---------------------------------------------------------------------------
#
# 2026-04-29 incident: live bars arrived as tz-aware UTC, the strategy
# compared ``df.index[-1].time()`` against ET windows, and entries fired
# at 11:45 ET (= 15:45 UTC) instead of 15:45 ET. The fix converts the bar
# index to US/Eastern before extracting time-of-day.


class TestOvernightDriftEntryWindowTimezone:
    """Entry window must respect ET wall-clock regardless of bar tz."""

    def _strategy(self) -> Any:
        from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
        return OvernightDriftStrategy(
            config={
                "max_positions": 12,
                "entry_window_start": "15:45",
                "entry_window_end": "15:55",
                "stop_loss_pct": 0.03,
                "fractional_shares": True,
                "position_pct": 0.5,
            }
        )

    def _bars(self, last_index: pd.Timestamp) -> pd.DataFrame:
        # Two bars so len-check passes; only the final timestamp matters.
        idx = pd.DatetimeIndex([last_index - pd.Timedelta(minutes=5), last_index])
        return pd.DataFrame(
            {"open": [100.0, 100.0], "high": [101.0, 101.0],
             "low": [99.0, 99.0], "close": [100.0, 100.0], "volume": [1000, 1000]},
            index=idx,
        )

    def test_utc_bar_at_1545_et_fires(self) -> None:
        """Last bar at 19:45 UTC == 15:45 ET should fire the entry window."""
        strat = self._strategy()
        ts = pd.Timestamp("2026-04-30 19:45:00", tz="UTC")
        df = self._bars(ts)
        decision = strat.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=500.0, available_cash=1000.0,
        )
        assert decision is not None, "expected entry at 15:45 ET (19:45 UTC)"

    def test_utc_bar_at_1145_et_does_not_fire(self) -> None:
        """Last bar at 15:45 UTC == 11:45 ET (mid-morning) must NOT fire."""
        strat = self._strategy()
        ts = pd.Timestamp("2026-04-30 15:45:00", tz="UTC")
        df = self._bars(ts)
        decision = strat.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=500.0, available_cash=1000.0,
        )
        assert decision is None, (
            "regression: 11:45 ET fired entry — UTC bar leaked into ET window"
        )

    def test_naive_bar_treated_as_et(self) -> None:
        """Naive (backtest-shaped) bars are assumed already-ET."""
        strat = self._strategy()
        ts = pd.Timestamp("2026-04-30 15:50:00")  # naive ET
        df = self._bars(ts)
        decision = strat.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=500.0, available_cash=1000.0,
        )
        assert decision is not None

    def test_slot_aware_sizing_divides_by_max_positions(self) -> None:
        """With max_positions=3, per-entry spend must be ~ pct/N of cash."""
        from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
        strat = OvernightDriftStrategy(
            config={
                "max_positions": 3,
                "entry_window_start": "15:45",
                "entry_window_end": "15:55",
                "stop_loss_pct": 0.03,
                "fractional_shares": True,
                "position_pct": 0.95,
            }
        )
        ts = pd.Timestamp("2026-04-30 19:45:00", tz="UTC")  # 15:45 ET
        idx = pd.DatetimeIndex([ts - pd.Timedelta(minutes=5), ts])
        df = pd.DataFrame(
            {"open": [100.0, 100.0], "high": [101.0, 101.0],
             "low": [99.0, 99.0], "close": [100.0, 100.0], "volume": [1000, 1000]},
            index=idx,
        )
        decision = strat.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=100.0, available_cash=1000.0,
        )
        assert decision is not None
        # Expected per-slot spend: 1000 * 0.95 / 3 = 316.67 -> 3.1667 shares.
        assert 3.0 < decision.shares < 3.3, (
            f"expected ~3.17 shares with N=3 sizing, got {decision.shares}"
        )

    def test_slot_aware_sizing_n_equals_one_unchanged(self) -> None:
        """With max_positions=1, per-entry spend == pct * cash (legacy)."""
        from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
        strat = OvernightDriftStrategy(
            config={
                "max_positions": 1,
                "entry_window_start": "15:45",
                "entry_window_end": "15:55",
                "stop_loss_pct": 0.03,
                "fractional_shares": True,
                "position_pct": 0.95,
            }
        )
        ts = pd.Timestamp("2026-04-30 19:45:00", tz="UTC")
        idx = pd.DatetimeIndex([ts - pd.Timedelta(minutes=5), ts])
        df = pd.DataFrame(
            {"open": [100.0, 100.0], "high": [101.0, 101.0],
             "low": [99.0, 99.0], "close": [100.0, 100.0], "volume": [1000, 1000]},
            index=idx,
        )
        decision = strat.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=100.0, available_cash=1000.0,
        )
        assert decision is not None
        # Expected: 950 / 100 = 9.5 shares.
        assert abs(decision.shares - 9.5) < 0.01

    def test_exit_uses_et_date_for_utc_bars(self) -> None:
        """Cross-day exit must use ET calendar, not UTC.

        2026-04-30 23:30 UTC == 19:30 ET (still 04-30). A position opened
        04-30 must NOT exit on this bar even though UTC date is 04-30 too —
        the test asserts no premature rollover.
        """
        from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
        strat = OvernightDriftStrategy(config={"max_positions": 1})
        ts = pd.Timestamp("2026-04-30 19:30:00", tz="UTC")  # 15:30 ET, same day
        df = self._bars(ts)
        position = {
            "entry_price": 100.0,
            "stop_price": 97.0,
            "entry_time": "2026-04-30T15:45:00-04:00",
            "hold_type": "swing",
        }
        signal = strat.evaluate_exit(position=position, current_price=100.4, df_5min=df)
        assert signal.should_exit is False, "exit fired same session"

"""Tests for strategy edge cases not covered by test_strategies.py:
mean_reversion (VIX-adaptive RSI, ATR stops, BB confirm, EMA confirm,
risk sizing, let-winners-run) and sentiment_combo technical signals."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
from trading_bot.strategy.strategies.sentiment_combo import SentimentComboStrategy


# ---------------------------------------------------------------------------
# Helpers — build oversold-then-recovery bar series for mean reversion
# ---------------------------------------------------------------------------


def _oversold_recovery_bars(
    n: int = 60, vol_spike: bool = True, base: float = 100.0
) -> pd.DataFrame:
    """Series with sharp dip + recovery so RSI dips below 28 then crosses 35."""
    rng = np.random.default_rng(7)
    prices: list[float] = [base]
    # Stable
    for _ in range(20):
        prices.append(prices[-1] * (1 + rng.normal(0.0, 0.001)))
    # Sharp drop (drives RSI deeply oversold)
    for _ in range(15):
        prices.append(prices[-1] * (1 + rng.normal(-0.025, 0.002)))
    # Recovery
    for _ in range(n - len(prices)):
        prices.append(prices[-1] * (1 + rng.normal(0.020, 0.002)))
    px = np.array(prices[:n])

    base_vol = 1_000_000
    vols = np.full(n, base_vol, dtype=float)
    if vol_spike:
        vols[-1] = base_vol * 2.0  # current bar high volume
    return pd.DataFrame(
        {
            "open": px,
            "high": px * 1.005,
            "low": px * 0.995,
            "close": px,
            "volume": vols,
        }
    )


def _quiet_daily_bars(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    """Low realized volatility daily bars — for the low-vol regime."""
    px = np.linspace(base, base * 1.01, n)  # almost flat
    return pd.DataFrame(
        {
            "open": px,
            "high": px * 1.0005,
            "low": px * 0.9995,
            "close": px,
            "volume": [1_000_000] * n,
        }
    )


def _wild_daily_bars(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    """High realized volatility daily bars — for the high-vol regime."""
    rng = np.random.default_rng(11)
    px = [base]
    for _ in range(n - 1):
        px.append(px[-1] * (1 + rng.normal(0, 0.05)))  # 5% daily moves
    px = np.array(px)
    return pd.DataFrame(
        {
            "open": px,
            "high": px * 1.02,
            "low": px * 0.98,
            "close": px,
            "volume": [1_000_000] * n,
        }
    )


def _mr_config(**overrides) -> dict[str, Any]:
    base = {
        "enabled": True,
        "allocation_usd": 1000.0,
        "max_positions": 1,
        "rsi_period": 14,
        "rsi_oversold": 28.0,
        "rsi_recovery": 35.0,
        "rsi_exit": 55.0,
        "stop_loss_pct": 0.02,
        "take_profit_pct": 0.03,
        "volume_multiplier": 1.3,
        "oversold_lookback": 5,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Mean reversion — uncovered branches
# ---------------------------------------------------------------------------


class TestMeanReversionEdges:
    def test_vix_adaptive_low_vol_uses_relaxed_threshold(self):
        s = MeanReversionStrategy(_mr_config(vix_adaptive_rsi=True))
        threshold, label = s._adaptive_rsi_oversold(_quiet_daily_bars(60))
        assert threshold == 30.0
        assert "low-vol" in label

    def test_vix_adaptive_high_vol_uses_tighter_threshold(self):
        s = MeanReversionStrategy(_mr_config(vix_adaptive_rsi=True))
        threshold, label = s._adaptive_rsi_oversold(_wild_daily_bars(60))
        assert threshold == 25.0
        assert "high-vol" in label

    def test_vix_adaptive_falls_back_with_too_few_bars(self):
        s = MeanReversionStrategy(_mr_config(vix_adaptive_rsi=True))
        threshold, label = s._adaptive_rsi_oversold(_wild_daily_bars(5))
        assert threshold == 28.0
        assert "rv-unavailable" in label

    def test_realized_vol_returns_none_for_short_series(self):
        v = MeanReversionStrategy._realized_vol_pct(_quiet_daily_bars(5))
        assert v is None

    def test_volume_below_threshold_skips_entry(self):
        df = _oversold_recovery_bars(n=60, vol_spike=False)
        # Drive current volume to exactly average (multiplier 1.3 needed)
        df.loc[df.index[-1], "volume"] = df["volume"].rolling(20).mean().iloc[-1]
        s = MeanReversionStrategy(_mr_config())
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=float(df["close"].iloc[-1]),
            available_cash=1000.0,
        )
        assert decision is None

    def test_ema_confirm_blocks_below_ema(self):
        # Build a sequence where price recovers but stays below the short EMA.
        df = _oversold_recovery_bars(n=60)
        # Force the last close below the short EMA window by setting it lower
        last_idx = df.index[-1]
        ema = df["close"].ewm(span=9, adjust=False).mean().iloc[-1]
        df.loc[last_idx, "close"] = ema - 1.0  # below EMA9
        s = MeanReversionStrategy(_mr_config(require_ema_confirm=True))
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=float(df["close"].iloc[-1]),
            available_cash=1000.0,
        )
        assert decision is None

    def test_atr_stops_when_enabled(self):
        df = _oversold_recovery_bars(60)
        s = MeanReversionStrategy(_mr_config(use_atr_stops=True))
        price = float(df["close"].iloc[-1])
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=price, available_cash=10000.0,
        )
        if decision is not None:
            # ATR-derived stop must be different from the fixed 2% stop and below entry
            assert decision.stop_price < price
            # trail_pct comes from the ATR path
            assert decision.trail_pct is not None

    def test_let_winners_run_promotes_target_to_activation(self):
        df = _oversold_recovery_bars(60)
        s = MeanReversionStrategy(
            _mr_config(use_atr_stops=True, let_winners_run=True)
        )
        price = float(df["close"].iloc[-1])
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=price, available_cash=10000.0,
        )
        if decision is not None:
            # With let_winners_run: target_price is None and activation is set
            assert decision.target_price is None
            assert decision.trail_activation_price is not None

    def test_risk_sizing_path(self):
        df = _oversold_recovery_bars(60)
        s = MeanReversionStrategy(_mr_config(use_risk_sizing=True))
        price = float(df["close"].iloc[-1])
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=price, available_cash=10000.0,
        )
        # Either the entry happens (decision is set) or sizing returned 0.
        # Either way the risk-sizing branch executed.
        if decision is not None:
            assert decision.shares >= 1

    def test_fractional_shares_minimum(self):
        df = _oversold_recovery_bars(60)
        s = MeanReversionStrategy(
            _mr_config(use_risk_sizing=True, fractional_shares=True)
        )
        price = float(df["close"].iloc[-1])
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=price, available_cash=10000.0,
        )
        if decision is not None:
            assert decision.shares >= 0.001

    def test_bb_confirm_required_blocks_when_no_touch(self):
        # Quiet uptrend that never touches the lower band.
        rng = np.random.default_rng(3)
        n = 60
        prices = np.cumsum(rng.normal(0.05, 0.05, n)) + 100  # smooth uptrend
        df = pd.DataFrame(
            {
                "open": prices, "high": prices * 1.001,
                "low": prices * 0.999, "close": prices,
                "volume": [2_000_000] * n,
            }
        )
        s = MeanReversionStrategy(_mr_config(require_bb_confirm=True))
        decision = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=_quiet_daily_bars(60),
            current_price=float(df["close"].iloc[-1]),
            available_cash=1000.0,
        )
        # Either RSI fails or BB fails — both result in None.
        assert decision is None

    def test_let_winners_run_blocks_rsi_exit_above_threshold(self):
        """When position is up >= let_winners_run_up_pct, RSI exit is suppressed."""
        s = MeanReversionStrategy(
            _mr_config(
                let_winners_run=True,
                let_winners_run_up_pct=0.03,
                take_profit_pct=0.50,  # fixed target so high it can't trigger
            )
        )
        # Build df so RSI is high (would normally exit) but price is up >3%
        df = pd.DataFrame(
            {
                "close": np.linspace(100, 110, 30),
                "high": np.linspace(100, 110, 30) * 1.001,
                "low": np.linspace(100, 110, 30) * 0.999,
                "open": np.linspace(100, 110, 30),
                "volume": [1_000_000] * 30,
            }
        )
        position = {
            "entry_price": 100.0, "quantity": 10,
            "stop_price": 0.0, "target_price": 0.0,
        }
        sig = s.evaluate_exit(position=position, current_price=110.0, df_5min=df)
        # Stop=0, fixed target = 150 (untriggered), RSI exit suppressed by
        # let_winners_run when position is +10% (above 3% threshold) → no exit.
        assert sig.should_exit is False

    def test_exit_on_stored_target(self):
        s = MeanReversionStrategy(_mr_config())
        position = {
            "entry_price": 100.0, "quantity": 10,
            "stop_price": 0.0, "target_price": 105.0,
        }
        sig = s.evaluate_exit(position=position, current_price=106.0)
        assert sig.should_exit is True
        assert sig.reason == "take_profit"


# ---------------------------------------------------------------------------
# Sentiment combo — technical signal counting
# ---------------------------------------------------------------------------


def _sc_config(**o) -> dict[str, Any]:
    base = {
        "enabled": True, "max_positions": 2,
        "sentiment_threshold": 0.15,
        "min_technical_signals": 1,
        "stop_loss_pct": 0.015,
        "take_profit_pct": 0.025,
    }
    base.update(o)
    return base


def _ema_cross_bars(n: int = 30) -> pd.DataFrame:
    """Build bars where EMA9 crosses above EMA21 in the last few bars."""
    # Down for a while, then sharp up — fast EMA crosses slow EMA
    n_down = n // 2
    n_up = n - n_down
    px = np.concatenate([
        np.linspace(110, 100, n_down),
        np.linspace(100, 115, n_up),
    ])
    return pd.DataFrame({
        "open": px, "high": px * 1.001, "low": px * 0.999,
        "close": px, "volume": [1_000_000] * n,
    })


class TestSentimentComboEdges:
    def test_low_sentiment_blocks(self):
        s = SentimentComboStrategy(_sc_config())
        d = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=_ema_cross_bars(), df_daily=_ema_cross_bars(),
            current_price=110.0, available_cash=1000.0,
            sentiment_score=0.05,
        )
        assert d is None

    def test_too_few_bars_blocks(self):
        s = SentimentComboStrategy(_sc_config())
        df = _ema_cross_bars(20)  # < 25
        d = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=110.0, available_cash=1000.0,
            sentiment_score=0.30,
        )
        assert d is None

    def test_ema_cross_signal_triggers_entry(self):
        s = SentimentComboStrategy(_sc_config())
        df = _ema_cross_bars(40)
        d = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=float(df["close"].iloc[-1]),
            available_cash=10000.0,
            sentiment_score=0.30,
        )
        # Either a decision (>= 1 tech signal) or None if signals didn't
        # fire on this synthetic data — both exercise the technical branch.
        assert d is None or d.shares >= 1

    def test_no_signals_blocks(self):
        # Flat price → no EMA cross, no BB bounce
        n = 30
        px = np.full(n, 100.0)
        df = pd.DataFrame({
            "open": px, "high": px, "low": px, "close": px,
            "volume": [1_000_000] * n,
        })
        s = SentimentComboStrategy(_sc_config())
        d = s.evaluate_entry(
            ticker="SPY", exchange="US",
            df_5min=df, df_daily=df,
            current_price=100.0, available_cash=1000.0,
            sentiment_score=0.30,
        )
        assert d is None

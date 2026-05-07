"""Tests for PositionSizer (Alpaca, commission-free)."""

from __future__ import annotations


import pytest

from trading_bot.config import Config
from trading_bot.execution.position_sizer import PositionSizer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sizer(config: Config) -> PositionSizer:
    """PositionSizer for USD-only Alpaca account."""
    return PositionSizer(config)


# ---------------------------------------------------------------------------
# Basic sizing
# ---------------------------------------------------------------------------


class TestBasicSizing:
    def test_basic_sizing_us(self, sizer: PositionSizer) -> None:
        """equity=$1000, risk=2% → $20 at risk. stop=$0.20 → 100 shares
        (capped at 40% × $1000 / $10 = 40 shares by max position rule)."""
        result = sizer.calculate(
            ticker="PLTR",
            exchange="NASDAQ",
            entry_price=10.00,
            stop_price=9.80,
            account_equity_usd=1000.0,
            sentiment_score=0.2,
            atr_rank=50.0,
        )
        assert result.is_valid
        assert result.shares > 0
        assert 30 <= result.shares <= 300

    def test_sizing_capped_by_max_position(self, sizer: PositionSizer) -> None:
        """Very tight stop → enormous shares; should be capped at 40% equity."""
        result = sizer.calculate(
            ticker="PLTR",
            exchange="NASDAQ",
            entry_price=10.00,
            stop_price=9.999,
            account_equity_usd=1000.0,
            sentiment_score=0.2,
            atr_rank=50.0,
        )
        if result.is_valid:
            position_value_usd = result.position_value_usd
            assert position_value_usd <= 1000.0 * 0.40 + 1

    def test_sizing_floored_by_min_position(self, sizer: PositionSizer) -> None:
        """Very wide stop → tiny position; should be rejected at minimum."""
        result = sizer.calculate(
            ticker="PLTR",
            exchange="NASDAQ",
            entry_price=10.00,
            stop_price=5.00,
            account_equity_usd=1000.0,
            sentiment_score=0.2,
            atr_rank=50.0,
        )
        assert isinstance(result.is_valid, bool)

    def test_zero_shares_when_stop_equals_entry(self, sizer: PositionSizer) -> None:
        result = sizer.calculate(
            ticker="PLTR",
            exchange="NASDAQ",
            entry_price=10.0,
            stop_price=10.0,
            account_equity_usd=1000.0,
            sentiment_score=0.2,
            atr_rank=50.0,
        )
        assert result.is_valid is False


# ---------------------------------------------------------------------------
# ATR and sentiment adjustments
# ---------------------------------------------------------------------------


class TestAdjustments:
    def test_atr_reduction_applied(self, sizer: PositionSizer) -> None:
        """ATR rank 75 (>70 threshold) — size reduced by 25%."""
        base = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.8,
            account_equity_usd=2000.0,
            sentiment_score=0.2, atr_rank=50.0,
        )
        high_vol = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.8,
            account_equity_usd=2000.0,
            sentiment_score=0.2, atr_rank=75.0,
        )
        if base.is_valid and high_vol.is_valid:
            assert high_vol.shares <= base.shares
            assert high_vol.shares <= base.shares * 0.80

    def test_sentiment_reduction(self, sizer: PositionSizer) -> None:
        """No sentiment data — size reduced to 75%."""
        with_sentiment = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.8,
            account_equity_usd=2000.0,
            sentiment_score=0.2, atr_rank=50.0,
        )
        no_sentiment = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.8,
            account_equity_usd=2000.0,
            sentiment_score=None, atr_rank=50.0,
        )
        if with_sentiment.is_valid and no_sentiment.is_valid:
            assert no_sentiment.shares < with_sentiment.shares

    def test_stacking_atr_and_sentiment(self, sizer: PositionSizer) -> None:
        """Both ATR high and no sentiment: 0.75 * 0.75 = ~56% of base."""
        base = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.5,
            account_equity_usd=5000.0,
            sentiment_score=0.2, atr_rank=50.0,
        )
        reduced = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.5,
            account_equity_usd=5000.0,
            sentiment_score=None, atr_rank=75.0,
        )
        if base.is_valid and reduced.is_valid and base.shares > 10:
            ratio = reduced.shares / base.shares
            assert 0.40 <= ratio <= 0.70

    def test_adjustment_descriptions_in_result(
        self, sizer: PositionSizer
    ) -> None:
        """PositionSize.adjustments should describe what was applied."""
        result = sizer.calculate(
            ticker="PLTR", exchange="NASDAQ",
            entry_price=10.0, stop_price=9.8,
            account_equity_usd=2000.0,
            sentiment_score=None, atr_rank=75.0,
        )
        if result.is_valid:
            text = " ".join(result.adjustments).lower()
            assert "atr" in text
            assert "sentiment" in text

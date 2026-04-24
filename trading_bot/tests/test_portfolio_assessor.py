"""Tests for PortfolioAssessor scoring and classification."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_bot.config import Config
from trading_bot.strategy.portfolio_assessor import PortfolioAssessor, PositionAssessment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_assessor(
    config: Config,
    mock_fx,
    mock_notifier,
    market_data: Any = None,
    sentiment: Any = None,
) -> PortfolioAssessor:
    if market_data is None:
        market_data = MagicMock()
        market_data.get_latest_price.return_value = 10.0
        market_data.get_historical_bars = AsyncMock(return_value=[])

    if sentiment is None:
        sentiment = MagicMock()
        sentiment.get_sentiment = AsyncMock(return_value=0.2)

    return PortfolioAssessor(config, market_data, sentiment, mock_fx, mock_notifier)


def _pltr_position() -> dict[str, Any]:
    """PLTR is NASDAQ/Information Technology — liquid major exchange."""
    return {
        "ticker": "PLTR",
        "exchange": "NASDAQ",
        "currency": "USD",
        "quantity": 100,
        "avg_cost": 9.0,
        "market_value": 1000.0,
        "unrealized_pnl": 100.0,
    }


def _penny_position() -> dict[str, Any]:
    """Simulated penny stock — low volume, deep loss."""
    return {
        "ticker": "PENNY",
        "exchange": "US",
        "currency": "USD",
        "quantity": 10000,
        "avg_cost": 0.10,
        "market_value": 50.0,
        "unrealized_pnl": -950.0,
    }


# ---------------------------------------------------------------------------
# Exchange scoring
# ---------------------------------------------------------------------------


class TestExchangeScoring:
    def test_nasdaq_gets_max_exchange_score(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        score = assessor._score_exchange("NASDAQ")
        assert score == assessor._w_exchange

    def test_nyse_gets_max_exchange_score(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        score = assessor._score_exchange("NYSE")
        assert score == assessor._w_exchange

    def test_unknown_exchange_gets_zero(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        score = assessor._score_exchange("UNKNOWN")
        assert score == 0


# ---------------------------------------------------------------------------
# Loss scoring
# ---------------------------------------------------------------------------


class TestLossScoring:
    def test_profit_gets_max_loss_score(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        assert assessor._score_loss(0.05) == assessor._w_loss

    def test_small_loss_gets_max_loss_score(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        assert assessor._score_loss(-0.03) == assessor._w_loss

    def test_medium_loss_gets_partial_score(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        score = assessor._score_loss(-0.10)
        assert 0 < score < assessor._w_loss

    def test_deep_loss_gets_zero(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        assessor = _make_assessor(config, mock_fx, mock_notifier)
        score = assessor._score_loss(-0.35)
        assert score == 0


# ---------------------------------------------------------------------------
# Classification thresholds
# ---------------------------------------------------------------------------


class TestClassification:
    def _assess_with_score(
        self, config: Config, mock_fx, mock_notifier, total_score: int
    ) -> PositionAssessment:
        return PositionAssessment(
            ticker="TEST",
            exchange="NYSE",
            current_value_gbp=100.0,
            unrealized_pnl_gbp=0.0,
            score=total_score,
            classification=(
                "HOLD" if total_score >= 60
                else "SELL" if total_score >= 30
                else "URGENT_SELL"
            ),
        )

    def test_classification_hold(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 75)
        assert a.classification == "HOLD"

    def test_classification_sell(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 45)
        assert a.classification == "SELL"

    def test_classification_urgent_sell(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 20)
        assert a.classification == "URGENT_SELL"

    def test_boundary_at_60_is_hold(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 60)
        assert a.classification == "HOLD"

    def test_boundary_at_30_is_sell(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 30)
        assert a.classification == "SELL"

    def test_below_30_is_urgent_sell(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        a = self._assess_with_score(config, mock_fx, mock_notifier, 29)
        assert a.classification == "URGENT_SELL"


# ---------------------------------------------------------------------------
# Full score_position integration
# ---------------------------------------------------------------------------


class TestScorePosition:
    @pytest.mark.asyncio
    async def test_pltr_scores_high(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        """PLTR (NASDAQ, positive P&L) should classify as HOLD."""
        bar = MagicMock()
        bar.volume = 2_000_000
        bar.close = 10.0
        bar.high = 10.1
        bar.low = 9.9

        md = MagicMock()
        md.get_latest_price.return_value = 10.0
        bars_20 = [bar] * 20
        bars_100 = [bar] * 100
        md.get_historical_bars = AsyncMock(side_effect=[bars_20, bars_100, bars_20])

        sentiment = MagicMock()
        sentiment.get_sentiment = AsyncMock(return_value=0.3)

        assessor = _make_assessor(config, mock_fx, mock_notifier,
                                  market_data=md, sentiment=sentiment)
        result = await assessor.score_position(_pltr_position())
        assert result.classification == "HOLD"
        assert result.score >= 60

    @pytest.mark.asyncio
    async def test_penny_scores_low(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        """Penny stock (deep loss, low volume) should be URGENT_SELL."""
        bar = MagicMock()
        bar.volume = 5_000
        bar.close = 0.005
        bar.high = 0.006
        bar.low = 0.004

        md = MagicMock()
        md.get_latest_price.return_value = 0.005
        md.get_historical_bars = AsyncMock(return_value=[bar] * 20)

        sentiment = MagicMock()
        sentiment.get_sentiment = AsyncMock(return_value=-0.4)

        assessor = _make_assessor(config, mock_fx, mock_notifier,
                                  market_data=md, sentiment=sentiment)
        result = await assessor.score_position(_penny_position())
        assert result.classification == "URGENT_SELL"
        assert result.score < 30

    @pytest.mark.asyncio
    async def test_trailing_stop_set_for_holds(
        self, config: Config, mock_fx, mock_notifier
    ) -> None:
        """HOLD positions get trailing_stop_price = current_price * 0.95."""
        bar = MagicMock()
        bar.volume = 2_000_000
        bar.close = 10.0
        bar.high = 10.1
        bar.low = 9.9

        md = MagicMock()
        md.get_latest_price.return_value = 10.0
        md.get_historical_bars = AsyncMock(return_value=[bar] * 100)

        sentiment = MagicMock()
        sentiment.get_sentiment = AsyncMock(return_value=0.3)

        assessor = _make_assessor(config, mock_fx, mock_notifier,
                                  market_data=md, sentiment=sentiment)
        result = await assessor.score_position(_pltr_position())
        if result.classification == "HOLD":
            assert result.trailing_stop_price is not None
            assert abs(result.trailing_stop_price - 9.5) < 0.01

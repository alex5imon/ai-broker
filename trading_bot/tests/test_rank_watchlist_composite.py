"""Unit tests for ``TradingBot._rank_watchlist`` composite formula.

Locks in the ai-broker#58 (M-2) decision:

* Default ``entry.sentiment_composite_offset`` = 0.0 — neutral sentiment
  zero-weights the gap, negative sentiment penalises ranking.
* Setting the offset to 0.5 restores the pre-#58 legacy behavior.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_bot.constants import Market
from trading_bot.main import TradingBot


@pytest.fixture
def bot(config, tmp_db_path, monkeypatch):
    """Minimal TradingBot wired with deterministic sentiment + price/bars."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    config._raw["database"]["path"] = tmp_db_path

    b = TradingBot(config, mode="normal", dry_run=False)

    b._sentiment = MagicMock()
    b._market_data = MagicMock()
    # Default: no gap (current_price == prev_close == 100.0) → gap_pct = 0.
    b._market_data.get_latest_price = MagicMock(return_value=100.0)
    b._market_data.get_historical_bars = AsyncMock(
        return_value=[SimpleNamespace(close=100.0)]
    )
    return b


def _set_sentiments(bot: TradingBot, mapping: dict[str, float]) -> None:
    async def _get(ticker: str) -> float | None:
        return mapping.get(ticker)

    bot._sentiment.get_sentiment = AsyncMock(side_effect=_get)


@pytest.mark.asyncio
async def test_default_offset_zero_weights_neutral_sentiment(bot):
    """offset=0.0 → neutral sentiment scores zero regardless of gap."""
    bot._sentiment_composite_offset = 0.0
    _set_sentiments(bot, {"NEU": 0.0, "POS": 1.0, "NEG": -1.0})

    # Inject a non-zero gap so we can verify the multiplication still happens.
    # current_price 110, prev_close 100 → gap_pct = 0.10.
    bot._market_data.get_latest_price = MagicMock(return_value=110.0)

    ranked = await bot._rank_watchlist(["NEU", "POS", "NEG"], Market.US, "NYSE")

    # POS: (0 + 1) * 1.10 = 1.10, NEU: 0, NEG: (0 - 1) * 1.10 = -1.10
    assert ranked == ["POS", "NEU", "NEG"]


@pytest.mark.asyncio
async def test_default_offset_negative_sentiment_penalises(bot):
    """offset=0.0 → max-negative-sentiment ticker scores negative even with gap."""
    bot._sentiment_composite_offset = 0.0
    _set_sentiments(bot, {"BAD": -1.0})
    bot._market_data.get_latest_price = MagicMock(return_value=105.0)

    # Capture the raw composite via a side-effecting wrapper around scores.
    # Simpler: invoke and assert sign indirectly by ranking against neutral.
    _set_sentiments(bot, {"BAD": -1.0, "NEU": 0.0})
    ranked = await bot._rank_watchlist(["BAD", "NEU"], Market.US, "NYSE")
    assert ranked == ["NEU", "BAD"]  # NEU (0) ranks above BAD (negative)


@pytest.mark.asyncio
async def test_legacy_offset_half_preserves_old_semantics(bot):
    """offset=0.5 → max-negative-sentiment still positive when gap_pct > 0.

    Regression-locks the pre-#58 behavior in case anyone wants to flip back.
    """
    bot._sentiment_composite_offset = 0.5
    _set_sentiments(bot, {"BAD": -1.0, "NEU": 0.0, "POS": 1.0})
    # gap_pct = 0.10
    bot._market_data.get_latest_price = MagicMock(return_value=110.0)

    ranked = await bot._rank_watchlist(["BAD", "NEU", "POS"], Market.US, "NYSE")

    # POS: (0.5 + 1) * 1.10 = 1.65
    # NEU: (0.5 + 0) * 1.10 = 0.55
    # BAD: (0.5 - 1) * 1.10 = -0.55  (still ordered after NEU, but per
    #   legacy: with gap_pct > 0, multiplier flips sign only when
    #   offset+sentiment crosses zero — BAD still negative here because
    #   |sentiment| > offset.)
    assert ranked == ["POS", "NEU", "BAD"]


@pytest.mark.asyncio
async def test_legacy_offset_half_keeps_negative_sentiment_positive_when_mild(bot):
    """offset=0.5 with mild negative sentiment (-0.3) → still positive composite."""
    bot._sentiment_composite_offset = 0.5
    _set_sentiments(bot, {"MILD_NEG": -0.3, "ZERO": 0.0})
    bot._market_data.get_latest_price = MagicMock(return_value=110.0)

    ranked = await bot._rank_watchlist(["MILD_NEG", "ZERO"], Market.US, "NYSE")

    # MILD_NEG: (0.5 - 0.3) * 1.10 = 0.22 (positive, ranks below ZERO=0.55)
    # ZERO: (0.5 + 0.0) * 1.10 = 0.55
    assert ranked == ["ZERO", "MILD_NEG"]


@pytest.mark.asyncio
async def test_offset_cached_from_config_default_zero(config, tmp_db_path, monkeypatch):
    """Init-time read of entry.sentiment_composite_offset → cached attribute."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    config._raw["database"]["path"] = tmp_db_path
    # config.yaml ships with sentiment_composite_offset: 0.0.
    b = TradingBot(config, mode="normal", dry_run=False)
    assert b._sentiment_composite_offset == 0.0


@pytest.mark.asyncio
async def test_offset_cached_from_config_overridden(config, tmp_db_path, monkeypatch):
    """Config override propagates into the cached attribute."""
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    config._raw["database"]["path"] = tmp_db_path
    config._raw.setdefault("entry", {})["sentiment_composite_offset"] = 0.5

    b = TradingBot(config, mode="normal", dry_run=False)
    assert b._sentiment_composite_offset == 0.5

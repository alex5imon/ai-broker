"""Tests for RegimeFilter — bullish/bearish gate around the SPY 50-day SMA."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategy.regime_filter import RegimeFilter


def _bars_above_sma(n: int = 80, start: float = 100.0) -> pd.DataFrame:
    """Series strongly trending up — current close > SMA(50)."""
    prices = np.linspace(start, start * 1.5, n)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": [1_000_000] * n,
        }
    )


def _bars_below_sma(n: int = 80, start: float = 100.0) -> pd.DataFrame:
    """Series in a clear downtrend — current close < SMA(50)."""
    prices = np.linspace(start, start * 0.5, n)
    return pd.DataFrame(
        {
            "open": prices,
            "high": prices * 1.01,
            "low": prices * 0.99,
            "close": prices,
            "volume": [1_000_000] * n,
        }
    )


@pytest.mark.asyncio
async def test_disabled_filter_always_allows():
    async def fetch(_t, _e):
        return None

    rf = RegimeFilter(get_daily_bars=fetch, enabled=False)
    assert rf.enabled is False
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_bullish_regime_allows():
    async def fetch(_t, _e):
        return _bars_above_sma()

    rf = RegimeFilter(get_daily_bars=fetch, sma_period=50)
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_bearish_regime_blocks():
    async def fetch(_t, _e):
        return _bars_below_sma()

    rf = RegimeFilter(get_daily_bars=fetch, sma_period=50)
    assert await rf.allows_new_entries() is False


@pytest.mark.asyncio
async def test_insufficient_history_allows():
    async def fetch(_t, _e):
        return _bars_above_sma(n=10)  # < sma_period + 1

    rf = RegimeFilter(get_daily_bars=fetch, sma_period=50)
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_none_dataframe_allows():
    async def fetch(_t, _e):
        return None

    rf = RegimeFilter(get_daily_bars=fetch)
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_empty_dataframe_allows():
    async def fetch(_t, _e):
        return pd.DataFrame()

    rf = RegimeFilter(get_daily_bars=fetch)
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_data_error_fails_open():
    async def fetch(_t, _e):
        raise RuntimeError("network down")

    rf = RegimeFilter(get_daily_bars=fetch)
    # Fail-open: don't block on transient data errors
    assert await rf.allows_new_entries() is True


@pytest.mark.asyncio
async def test_result_cached_within_ttl():
    """Once computed, repeat calls within TTL skip the fetch."""
    calls = {"n": 0}

    async def fetch(_t, _e):
        calls["n"] += 1
        return _bars_above_sma()

    rf = RegimeFilter(get_daily_bars=fetch, cache_ttl_minutes=30)
    assert await rf.allows_new_entries() is True
    assert await rf.allows_new_entries() is True
    assert await rf.allows_new_entries() is True
    assert calls["n"] == 1  # cached after first call


@pytest.mark.asyncio
async def test_capitalized_close_column_supported():
    """Some data sources use 'Close' instead of 'close'."""
    df = _bars_above_sma()
    df = df.rename(columns={"close": "Close"})

    async def fetch(_t, _e):
        return df

    rf = RegimeFilter(get_daily_bars=fetch, sma_period=50)
    # Should still parse and return a verdict (True for bullish data)
    assert await rf.allows_new_entries() is True

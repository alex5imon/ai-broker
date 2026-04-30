"""Shared pytest fixtures for all trading bot tests."""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_bot.config import Config
from trading_bot.constants import HoldType, Market, Phase, Exchange

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    """Load real config.yaml."""
    return Config.load("config.yaml")


@pytest.fixture
def raw_config(config: Config) -> dict[str, Any]:
    """Return the raw config dict."""
    return config._raw


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Apply the full schema to a connection."""
    from trading_bot.db.schema import _SCHEMA_SQL, _SEED_VERSION_SQL
    from trading_bot.constants import SCHEMA_VERSION

    conn.executescript(_SCHEMA_SQL)
    conn.execute(_SEED_VERSION_SQL, (SCHEMA_VERSION,))
    conn.commit()
    conn.row_factory = sqlite3.Row


@pytest.fixture
def tmp_db():
    """In-memory SQLite DB with full schema applied."""
    conn = sqlite3.connect(":memory:")
    _apply_schema(conn)
    yield conn
    conn.close()


@pytest.fixture
def tmp_db_path(tmp_path):
    """Path to a temp file-based DB with full schema.  Used by classes that open
    their own connections (RiskManager, etc.)."""
    db_file = tmp_path / "test_bot.db"
    conn = sqlite3.connect(str(db_file))
    _apply_schema(conn)
    conn.close()
    return str(db_file)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


def _make_ohlcv(
    n: int,
    start_price: float = 10.0,
    trend: float = 0.0,
    volatility: float = 0.005,
    base_volume: float = 500_000,
    rng: np.random.Generator | None = None,
) -> pd.DataFrame:
    """Generate synthetic OHLCV bars."""
    if rng is None:
        rng = np.random.default_rng(42)
    prices = [start_price]
    for _ in range(n - 1):
        change = rng.normal(trend, volatility)
        prices.append(prices[-1] * (1 + change))
    prices = np.array(prices)
    high = prices * (1 + rng.uniform(0.001, 0.005, n))
    low = prices * (1 - rng.uniform(0.001, 0.005, n))
    volume = rng.uniform(base_volume * 0.5, base_volume * 1.5, n)
    return pd.DataFrame(
        {
            "open": prices,
            "high": high,
            "low": low,
            "close": prices,
            "volume": volume,
        }
    )


@pytest.fixture
def sample_5min_bars() -> pd.DataFrame:
    """100 rows of realistic 5-min OHLCV data (uptrending with volume)."""
    return _make_ohlcv(100, start_price=10.0, trend=0.001, volatility=0.004)


@pytest.fixture
def sample_daily_bars() -> pd.DataFrame:
    """120 rows of daily OHLCV data for ATR calculation."""
    return _make_ohlcv(120, start_price=10.0, trend=0.0002, volatility=0.015)


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_gateway():
    """Mock Alpaca gateway connection — never hits the network."""
    gw = MagicMock()
    gw.is_connected = True
    gw.client = MagicMock()
    gw.get_positions = AsyncMock(return_value=[])
    gw.get_open_orders = AsyncMock(return_value=[])
    gw.get_account_summary = AsyncMock(
        return_value={
            "NetLiquidation": "1000.0",
            "SettledCash": "950.0",
            "BuyingPower": "950.0",
        }
    )
    return gw


@pytest.fixture
def mock_notifier():
    """Mock notifier that records calls.

    ``send_sync`` is genuinely synchronous on the real ``Notifier`` (see
    :func:`trading_bot.notifications.notifier.Notifier.send_sync`); using
    ``AsyncMock`` for that attribute would emit ``coroutine was never
    awaited`` warnings from the sync risk-manager paths.
    """
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=None)
    notifier.send_sync = MagicMock(return_value=None)
    notifier.gateway_alert = AsyncMock(return_value=None)
    notifier.drawdown_alert = AsyncMock(return_value=None)
    notifier.kill_switch = AsyncMock(return_value=None)
    return notifier


@pytest.fixture
def mock_market_data():
    """Mock MarketDataManager."""
    md = MagicMock()
    md.get_bid_ask.return_value = (9.95, 10.05)
    md.get_spread_pct.return_value = 0.001
    md.get_latest_price.return_value = 10.0
    md.trading_paused = False
    return md


@pytest.fixture
def mock_sentiment():
    """Mock SentimentAnalyzer."""
    sa = MagicMock()
    sa.get_sentiment = AsyncMock(return_value=0.2)
    return sa


@pytest.fixture
def mock_earnings():
    """Mock EarningsCalendar."""
    ec = MagicMock()
    ec.is_in_blackout.return_value = False
    return ec


@pytest.fixture
def mock_fx():
    """Mock FXManager with GBP/USD = 1.25."""
    fx = MagicMock()
    fx.get_rate.return_value = 1.25
    fx.rate = 1.25
    fx.is_live = True
    fx.to_gbp = lambda amount, currency: (
        amount if currency.upper() == "GBP"
        else (amount / 100.0 if currency.upper() == "GBX" else amount / 1.25)
    )
    fx.to_usd = lambda amount, currency: (
        amount if currency.upper() == "USD"
        else amount * 1.25
    )
    return fx



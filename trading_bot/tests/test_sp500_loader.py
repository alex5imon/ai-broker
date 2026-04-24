"""Tests for S&P 500 CSV data loader."""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from trading_bot.data.sp500_loader import (
    _DATA_DIR,
    get_date_range,
    list_tickers,
    load_ticker,
    load_ticker_range,
    load_universe,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _generate_csv(name: str, base_price: float, base_volume: int, n_rows: int = 60) -> str:
    """Generate a CSV string with n_rows of daily data starting 2017-01-03."""
    lines = ["date,open,high,low,close,volume,Name"]
    d = date(2017, 1, 3)
    count = 0
    while count < n_rows:
        if d.weekday() < 5:
            p = base_price + count * 0.1
            lines.append(
                f"{d.isoformat()},{p - 0.2:.1f},{p + 0.5:.1f},{p - 0.5:.1f},{p:.1f},{base_volume},{name}"
            )
            count += 1
        d += timedelta(days=1)
    return "\n".join(lines) + "\n"


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """Create a temp directory with fake CSV files for 3 normal + 1 low-vol ticker."""
    for ticker in ["AAPL", "MSFT", "GOOG"]:
        (tmp_path / f"{ticker}_data.csv").write_text(
            _generate_csv(ticker, base_price=10.0, base_volume=2_000_000, n_rows=60)
        )
    (tmp_path / "TINY_data.csv").write_text(
        _generate_csv("TINY", base_price=100.0, base_volume=50_000, n_rows=60)
    )
    return tmp_path


# ---------------------------------------------------------------------------
# list_tickers
# ---------------------------------------------------------------------------

class TestListTickers:
    def test_returns_sorted(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            tickers = list_tickers()
        assert tickers == ["AAPL", "GOOG", "MSFT", "TINY"]

    def test_empty_when_dir_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with patch("trading_bot.data.sp500_loader._DATA_DIR", missing):
            assert list_tickers() == []


# ---------------------------------------------------------------------------
# load_ticker
# ---------------------------------------------------------------------------

class TestLoadTicker:
    def test_loads_dataframe(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            df = load_ticker("AAPL")
        assert not df.empty
        assert list(df.columns) == ["open", "high", "low", "close", "volume"]
        assert df.index.name == "timestamp"
        assert len(df) == 60

    def test_missing_ticker_returns_empty(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            df = load_ticker("DOESNOTEXIST")
        assert df.empty

    def test_sorted_by_date(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            df = load_ticker("MSFT")
        assert (df.index == df.index.sort_values()).all()


# ---------------------------------------------------------------------------
# load_ticker_range
# ---------------------------------------------------------------------------

class TestLoadTickerRange:
    def test_filters_by_date(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            df = load_ticker_range("AAPL", date(2017, 1, 10), date(2017, 1, 20))
        assert 0 < len(df) < 60

    def test_empty_range(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            df = load_ticker_range("AAPL", date(2020, 1, 1), date(2020, 12, 31))
        assert df.empty


# ---------------------------------------------------------------------------
# load_universe
# ---------------------------------------------------------------------------

class TestLoadUniverse:
    def test_filters_by_volume(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            result = load_universe(
                date(2017, 1, 3), date(2017, 6, 1),
                min_avg_volume=500_000,
                min_price=5.0,
                max_price=500.0,
            )
        assert "TINY" not in result
        assert "AAPL" in result

    def test_filters_by_price(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            result = load_universe(
                date(2017, 1, 3), date(2017, 6, 1),
                min_avg_volume=0,
                min_price=50.0,
                max_price=999.0,
            )
        assert "AAPL" not in result
        assert "TINY" in result

    def test_returns_dict_of_dataframes(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            result = load_universe(
                date(2017, 1, 3), date(2017, 6, 1),
                min_avg_volume=0,
                min_price=0.0,
                max_price=9999.0,
            )
        assert isinstance(result, dict)
        for ticker, df in result.items():
            assert isinstance(df, pd.DataFrame)
            assert "close" in df.columns


# ---------------------------------------------------------------------------
# get_date_range
# ---------------------------------------------------------------------------

class TestGetDateRange:
    def test_returns_range(self, tmp_data_dir: Path) -> None:
        with patch("trading_bot.data.sp500_loader._DATA_DIR", tmp_data_dir):
            result = get_date_range()
        assert result is not None
        start, end = result
        assert start == date(2017, 1, 3)
        assert end > start

    def test_returns_none_when_no_data(self, tmp_path: Path) -> None:
        missing = tmp_path / "nonexistent"
        with patch("trading_bot.data.sp500_loader._DATA_DIR", missing):
            assert get_date_range() is None


# ---------------------------------------------------------------------------
# Integration: load real data if available
# ---------------------------------------------------------------------------

class TestRealData:
    @pytest.mark.skipif(
        not _DATA_DIR.exists(),
        reason="S&P 500 CSV dataset not present",
    )
    def test_list_tickers_real(self) -> None:
        tickers = list_tickers()
        assert len(tickers) > 400

    @pytest.mark.skipif(
        not _DATA_DIR.exists(),
        reason="S&P 500 CSV dataset not present",
    )
    def test_load_aapl_real(self) -> None:
        df = load_ticker("AAPL")
        assert not df.empty
        assert len(df) > 1000

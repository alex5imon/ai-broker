"""Load S&P 500 daily OHLCV data from the individual_stocks_5yr CSV dataset.

The dataset lives at ``trading_bot/individual_stocks_5yr/{TICKER}_data.csv``
with columns: ``date, open, high, low, close, volume, Name``.

Date range: ~2013-02-08 to 2018-02-07 (daily bars).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

logger: logging.Logger = logging.getLogger(__name__)

_DATA_DIR: Path = Path(__file__).resolve().parent.parent.parent / "backtest_data" / "individual_stocks_5yr"


def list_tickers() -> list[str]:
    """Return all available ticker symbols sorted alphabetically."""
    if not _DATA_DIR.exists():
        logger.warning("S&P 500 data directory not found: %s", _DATA_DIR)
        return []
    tickers: list[str] = []
    for f in _DATA_DIR.glob("*_data.csv"):
        ticker = f.stem.replace("_data", "")
        tickers.append(ticker)
    return sorted(tickers)


def load_ticker(ticker: str) -> pd.DataFrame:
    """Load full history for a single ticker.

    Returns a DataFrame with DatetimeIndex and columns:
    ``[open, high, low, close, volume]``.
    """
    path = _DATA_DIR / f"{ticker}_data.csv"
    if not path.exists():
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["date"])
    df = df.rename(columns={"date": "timestamp"})
    df = df.set_index("timestamp").sort_index()
    df = df[["open", "high", "low", "close", "volume"]]
    df = df.dropna(subset=["close"])
    return df


def load_ticker_range(
    ticker: str,
    from_date: date,
    to_date: date,
) -> pd.DataFrame:
    """Load daily bars for a ticker within a date range (inclusive)."""
    df = load_ticker(ticker)
    if df.empty:
        return df
    mask = (df.index >= pd.Timestamp(from_date)) & (df.index <= pd.Timestamp(to_date))
    return df.loc[mask]


def load_universe(
    from_date: date,
    to_date: date,
    min_avg_volume: int = 500_000,
    min_price: float = 5.0,
    max_price: float = 500.0,
) -> dict[str, pd.DataFrame]:
    """Load all S&P tickers and filter by liquidity/price criteria.

    Returns ``{ticker: daily_df}`` for tickers that pass all filters
    based on their average values over the date range.
    """
    all_tickers = list_tickers()
    logger.info(
        "Loading S&P 500 universe: %d tickers, %s to %s",
        len(all_tickers), from_date, to_date,
    )

    result: dict[str, pd.DataFrame] = {}
    skipped_volume: int = 0
    skipped_price: int = 0
    skipped_data: int = 0

    for ticker in all_tickers:
        df = load_ticker_range(ticker, from_date, to_date)
        if df.empty or len(df) < 50:
            skipped_data += 1
            continue

        avg_vol = float(df["volume"].mean())
        avg_price = float(df["close"].mean())

        if avg_vol < min_avg_volume:
            skipped_volume += 1
            continue
        if avg_price < min_price or avg_price > max_price:
            skipped_price += 1
            continue

        result[ticker] = df

    logger.info(
        "Universe loaded: %d tickers passed filters "
        "(skipped: %d low volume, %d price out of range, %d insufficient data)",
        len(result), skipped_volume, skipped_price, skipped_data,
    )
    return result


def get_date_range() -> tuple[date, date] | None:
    """Return the overall date range available in the dataset."""
    tickers = list_tickers()
    if not tickers:
        return None

    df = load_ticker(tickers[0])
    if df.empty:
        return None

    return df.index[0].date(), df.index[-1].date()

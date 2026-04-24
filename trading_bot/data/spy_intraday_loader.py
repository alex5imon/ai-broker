"""Load 1-minute SPY data and resample to 5-minute bars.

The dataset lives at ``backtest_data/1_min_SPY_2008-2021/1_min_SPY_2008-2021.csv``
with columns: ``index, date, open, high, low, close, volume, barCount, average``.

Date format: ``YYYYMMDD  HH:MM:SS`` (US/Eastern assumed).
Date range: 2008-01-22 to 2021-05-06 (~2M rows).
"""

from __future__ import annotations

import logging
from datetime import date, time
from pathlib import Path

import pandas as pd

logger: logging.Logger = logging.getLogger(__name__)

_CSV_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "backtest_data"
    / "1_min_SPY_2008-2021"
    / "1_min_SPY_2008-2021.csv"
)

_MARKET_OPEN: time = time(9, 30)
_MARKET_CLOSE: time = time(16, 0)

_df_cache: pd.DataFrame | None = None


def _load_raw() -> pd.DataFrame:
    """Load and cache the full CSV. Filters to regular trading hours."""
    global _df_cache
    if _df_cache is not None:
        return _df_cache

    if not _CSV_PATH.exists():
        logger.warning("SPY 1-min CSV not found: %s", _CSV_PATH)
        return pd.DataFrame()

    logger.info("Loading SPY 1-min CSV (this may take a moment)...")
    df = pd.read_csv(
        _CSV_PATH,
        usecols=["date", "open", "high", "low", "close", "volume"],
    )
    df["date"] = pd.to_datetime(df["date"].str.strip(), format="%Y%m%d  %H:%M:%S")
    df = df.rename(columns={"date": "timestamp"})
    df = df.set_index("timestamp").sort_index()

    df = df.between_time(_MARKET_OPEN, _MARKET_CLOSE)
    df = df[df["volume"] > 0]

    logger.info(
        "SPY data loaded: %d bars, %s to %s",
        len(df), df.index[0].date(), df.index[-1].date(),
    )
    _df_cache = df
    return df


def load_spy_range(
    from_date: date,
    to_date: date,
    resample: str = "5min",
) -> pd.DataFrame:
    """Load SPY bars for a date range, resampled to the given frequency.

    Returns a DataFrame with DatetimeIndex and columns:
    ``[open, high, low, close, volume]``.
    """
    df = _load_raw()
    if df.empty:
        return df

    mask = (df.index >= pd.Timestamp(from_date)) & (
        df.index < pd.Timestamp(to_date) + pd.Timedelta(days=1)
    )
    sliced = df.loc[mask]

    if sliced.empty:
        return sliced

    if resample == "1min":
        return sliced

    resampled = sliced.resample(resample, closed="left", label="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    return resampled.dropna(subset=["open", "close"])


def load_spy_day(target_date: date, resample: str = "5min") -> pd.DataFrame:
    """Load a single day of SPY bars."""
    return load_spy_range(target_date, target_date, resample=resample)


def get_trading_days(from_date: date, to_date: date) -> list[date]:
    """Return all trading days with SPY data in the given range."""
    df = _load_raw()
    if df.empty:
        return []

    mask = (df.index >= pd.Timestamp(from_date)) & (
        df.index < pd.Timestamp(to_date) + pd.Timedelta(days=1)
    )
    sliced = df.loc[mask]
    days = sorted(set(ts.date() for ts in sliced.index))
    return days


def get_date_range() -> tuple[date, date] | None:
    """Return the overall date range available."""
    df = _load_raw()
    if df.empty:
        return None
    return df.index[0].date(), df.index[-1].date()

"""Local parquet cache for backtest historical market data.

Caches intraday (1-min) and daily OHLCV DataFrames so that repeated
backtests over the same dates avoid redundant API calls.

Storage layout::

    data/cache/{TICKER}/{YYYY-MM-DD}_intraday.parquet
    data/cache/{TICKER}/{YYYY-MM-DD}_daily.parquet

The cache directory is created lazily on first write.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd

logger: logging.Logger = logging.getLogger(__name__)

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_CACHE_DIR: Path = _PROJECT_ROOT / "data" / "cache"


def get_cache_dir() -> Path:
    """Return the cache directory, creating it if needed."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def cache_key(ticker: str, target_date: date, bar_type: str) -> Path:
    """Return the path for a cached file.

    Parameters
    ----------
    ticker:
        Symbol string (e.g. ``"AAPL"``).
    target_date:
        The date used as the cache key.
    bar_type:
        ``"intraday"`` or ``"daily"``.
    """
    safe_ticker: str = ticker.replace("/", "_").replace("\\", "_")
    return _CACHE_DIR / safe_ticker / f"{target_date.isoformat()}_{bar_type}.parquet"


def load_cached(ticker: str, target_date: date, bar_type: str) -> pd.DataFrame | None:
    """Load cached data if it exists, return ``None`` otherwise.

    The returned DataFrame has a timezone-aware DatetimeIndex named
    ``"timestamp"`` and columns ``[open, high, low, close, volume]``.
    """
    path: Path = cache_key(ticker, target_date, bar_type)
    if not path.exists():
        return None

    try:
        df: pd.DataFrame = pd.read_parquet(path)
        logger.debug("Cache hit for %s %s %s", ticker, target_date.isoformat(), bar_type)
        return df
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Failed to read cache file %s: %s — treating as miss", path, exc
        )
        return None


def save_to_cache(
    ticker: str, target_date: date, bar_type: str, df: pd.DataFrame
) -> None:
    """Save a DataFrame to the cache.

    Creates the ticker subdirectory on first write.  The DataFrame index
    (timezone-aware datetime) is preserved in the parquet metadata.
    """
    if df.empty:
        logger.debug(
            "Skipping cache write for %s %s %s — empty DataFrame",
            ticker, target_date.isoformat(), bar_type,
        )
        return

    path: Path = cache_key(ticker, target_date, bar_type)
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        df.to_parquet(path, engine="pyarrow")
        logger.debug(
            "Cached %s %s %s (%d rows, %.1f KB)",
            ticker,
            target_date.isoformat(),
            bar_type,
            len(df),
            path.stat().st_size / 1024,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to write cache file %s: %s", path, exc)


def cache_stats() -> dict:
    """Return stats about the cache.

    Keys: ``file_count``, ``total_size_mb``, ``tickers``, ``date_range``.
    """
    stats: dict = {
        "file_count": 0,
        "total_size_bytes": 0,
        "total_size_mb": 0.0,
        "tickers": [],
        "date_range": None,
    }

    if not _CACHE_DIR.exists():
        return stats

    tickers: set[str] = set()
    dates: list[str] = []
    total_size: int = 0
    file_count: int = 0

    for parquet_file in _CACHE_DIR.rglob("*.parquet"):
        file_count += 1
        total_size += parquet_file.stat().st_size
        tickers.add(parquet_file.parent.name)

        # Extract date from filename like "2026-01-15_intraday.parquet"
        stem: str = parquet_file.stem  # "2026-01-15_intraday"
        date_part: str = stem.rsplit("_", maxsplit=1)[0]
        dates.append(date_part)

    stats["file_count"] = file_count
    stats["total_size_bytes"] = total_size
    stats["total_size_mb"] = round(total_size / (1024 * 1024), 2)
    stats["tickers"] = sorted(tickers)

    if dates:
        sorted_dates: list[str] = sorted(dates)
        stats["date_range"] = (sorted_dates[0], sorted_dates[-1])

    return stats

"""Download historical bars from Alpaca and store in the parquet cache.

Usage::

    python -m trading_bot.data.alpaca_downloader --from 2026-01-15 --to 2026-04-15

Downloads 1-min intraday bars for each trading day and daily bars (120-day
lookback) for all watchlist tickers. Results are saved to the same parquet
cache used by the backtester (``data/cache/{TICKER}/{DATE}_{type}.parquet``).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from trading_bot.config import Config
from trading_bot.data_cache import load_cached, save_to_cache

logger: logging.Logger = logging.getLogger(__name__)

TZ_EASTERN = ZoneInfo("US/Eastern")
TZ_UTC = ZoneInfo("UTC")


def _get_trading_days(start: date, end: date, config: Config) -> list[date]:
    """Return weekdays excluding holidays between start and end inclusive."""
    holidays_raw: dict[str, Any] = config._raw.get("holidays", {})
    holiday_dates: set[str] = set()
    for key, val in holidays_raw.items():
        if isinstance(val, list):
            for h in val:
                holiday_dates.add(str(h))

    days: list[date] = []
    d: date = start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holiday_dates:
            days.append(d)
        d += timedelta(days=1)
    return days


def _bars_to_df(bars: list) -> pd.DataFrame:
    """Convert Alpaca Bar objects to a DataFrame with DatetimeIndex."""
    if not bars:
        return pd.DataFrame()

    rows: list[dict[str, Any]] = []
    for bar in bars:
        rows.append({
            "timestamp": bar.timestamp,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": int(bar.volume),
        })

    df: pd.DataFrame = pd.DataFrame(rows)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    return df


async def download_ticker_day(
    client: StockHistoricalDataClient,
    ticker: str,
    target_date: date,
    feed: str = "iex",
) -> tuple[pd.DataFrame | None, str]:
    """Download 1-min bars for a single ticker on a single day.

    Returns (df, status) where status is 'cached', 'downloaded', or 'empty'.
    """
    cached: pd.DataFrame | None = load_cached(ticker, target_date, "intraday")
    if cached is not None and not cached.empty:
        return cached, "cached"

    start_dt: datetime = datetime(
        target_date.year, target_date.month, target_date.day,
        4, 0, 0, tzinfo=TZ_EASTERN,
    )
    end_dt: datetime = datetime(
        target_date.year, target_date.month, target_date.day,
        20, 0, 0, tzinfo=TZ_EASTERN,
    )

    try:
        from alpaca.data.enums import DataFeed, Adjustment
        feed_enum = DataFeed.IEX if feed.lower() == "iex" else DataFeed.SIP

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=start_dt,
            end=end_dt,
            feed=feed_enum,
            adjustment=Adjustment.ALL,  # split + dividend adjusted
        )
        response = client.get_stock_bars(request)
        bars = response.data.get(ticker, [])
    except Exception:
        logger.exception("Failed to download %s for %s", ticker, target_date)
        return None, "error"

    if not bars:
        return None, "empty"

    df: pd.DataFrame = _bars_to_df(bars)
    save_to_cache(ticker, target_date, "intraday", df)
    return df, "downloaded"


async def download_daily_bars(
    client: StockHistoricalDataClient,
    ticker: str,
    target_date: date,
    lookback_days: int = 120,
    feed: str = "iex",
) -> tuple[pd.DataFrame | None, str]:
    """Download daily bars ending on target_date with lookback.

    Returns (df, status).
    """
    cached: pd.DataFrame | None = load_cached(ticker, target_date, "daily")
    if cached is not None and not cached.empty:
        return cached, "cached"

    start_dt: datetime = datetime(
        target_date.year, target_date.month, target_date.day,
        tzinfo=TZ_UTC,
    ) - timedelta(days=lookback_days)
    end_dt: datetime = datetime(
        target_date.year, target_date.month, target_date.day,
        23, 59, 59, tzinfo=TZ_UTC,
    )

    try:
        from alpaca.data.enums import DataFeed, Adjustment
        feed_enum = DataFeed.IEX if feed.lower() == "iex" else DataFeed.SIP

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=start_dt,
            end=end_dt,
            feed=feed_enum,
            adjustment=Adjustment.ALL,
        )
        response = client.get_stock_bars(request)
        bars = response.data.get(ticker, [])
    except Exception:
        logger.exception("Failed to download daily bars for %s", ticker)
        return None, "error"

    if not bars:
        return None, "empty"

    df: pd.DataFrame = _bars_to_df(bars)
    save_to_cache(ticker, target_date, "daily", df)
    return df, "downloaded"


async def download_all(
    config: Config,
    tickers: list[str],
    from_date: date,
    to_date: date,
    rate_limit_per_min: int = 180,
) -> dict[str, int]:
    """Download intraday + daily bars for all tickers across the date range.

    Respects Alpaca's free-tier rate limit (~200 req/min).
    Returns a dict of status counts.
    """
    api_key: str = os.environ.get("ALPACA_API_KEY", "")
    secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")

    if not api_key or not secret_key:
        logger.error("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set")
        return {"error": 1}

    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret_key)
    feed: str = config._raw.get("alpaca", {}).get("data_feed", "iex")

    trading_days: list[date] = _get_trading_days(from_date, to_date, config)
    logger.info(
        "Downloading data for %d tickers x %d trading days (%s to %s)",
        len(tickers), len(trading_days),
        from_date.isoformat(), to_date.isoformat(),
    )

    stats: dict[str, int] = {"cached": 0, "downloaded": 0, "empty": 0, "error": 0}
    request_count: int = 0
    window_start: float = time.monotonic()

    for ticker in tickers:
        # Download daily bars once per ticker (for the last trading day, covers lookback)
        _, daily_status = await download_daily_bars(
            client, ticker, to_date, lookback_days=120, feed=feed,
        )
        stats[daily_status] = stats.get(daily_status, 0) + 1
        if daily_status == "downloaded":
            request_count += 1

        for day in trading_days:
            # Rate limiting
            if request_count >= rate_limit_per_min:
                elapsed: float = time.monotonic() - window_start
                if elapsed < 60:
                    wait: float = 60 - elapsed + 1
                    logger.info("Rate limit: waiting %.0fs...", wait)
                    await asyncio.sleep(wait)
                request_count = 0
                window_start = time.monotonic()

            _, status = await download_ticker_day(client, ticker, day, feed=feed)
            stats[status] = stats.get(status, 0) + 1
            if status == "downloaded":
                request_count += 1

            if request_count % 20 == 0 and request_count > 0:
                logger.info(
                    "Progress: %s — cached=%d downloaded=%d empty=%d error=%d",
                    ticker, stats["cached"], stats["downloaded"],
                    stats["empty"], stats["error"],
                )

    logger.info(
        "Download complete: cached=%d downloaded=%d empty=%d error=%d",
        stats["cached"], stats["downloaded"], stats["empty"], stats["error"],
    )
    return stats


async def main() -> None:
    """CLI entry point."""
    from trading_bot.log_setup import setup_logging

    log_path = setup_logging("alpaca_downloader")

    parser = argparse.ArgumentParser(description="Download Alpaca historical data to cache")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument("--tickers", nargs="*", help="Override tickers (default: watchlist from config)")
    args = parser.parse_args()

    from dotenv import load_dotenv
    load_dotenv()

    config = Config.load(args.config)
    d_from = date.fromisoformat(args.from_date)
    d_to = date.fromisoformat(args.to_date)

    if args.tickers:
        tickers = args.tickers
    else:
        raw = config._raw.get("watchlist", {})
        tickers = list(raw.get("us", []))

    stats = await download_all(config, tickers, d_from, d_to)
    print(f"\nDownload complete: {stats}")
    print(f"Log file: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())

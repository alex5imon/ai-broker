"""Download historical bars from Alpaca and store in the parquet cache.

Usage::

    python -m trading_bot.data.alpaca_downloader --from 2026-01-15 --to 2026-04-15

Downloads 1-min intraday bars for each trading day and daily bars (120-day
lookback) for all watchlist tickers. Results are saved to the same parquet
cache used by the backtester (``data/cache/{TICKER}/{DATE}_{type}.parquet``).

Pass ``--full-daily-history`` to write a per-trading-day daily parquet
across the full range (one file per trading day, each carrying the last
``lookback_days`` of trading-day bars ending on that date) instead of the
default behaviour of saving a single daily file at the endpoint. This is
required for multi-year backtests / walkforward windows where the regime
filter needs a continuous daily history to calibrate against, not just the
last 120 days before the run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from trading_bot.config import Config
from trading_bot.constants import TZ_EASTERN, TZ_UTC
from trading_bot.data_cache import load_cached, save_to_cache

logger: logging.Logger = logging.getLogger(__name__)


class DailyHistoryResult(NamedTuple):
    """Outcome of a :func:`download_daily_history` call.

    ``missed`` counts target trading days that were requested but produced
    an empty slice (typically: the target predates Alpaca's available
    history for the ticker, or the API returned no bars covering it).
    Callers should surface a non-zero ``missed`` count loudly — a missed
    day means the cache is incomplete, which is exactly the failure mode
    this helper exists to prevent.
    """

    written: int
    skipped: int
    missed: int
    requests_made: int


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


async def download_daily_history(
    client: StockHistoricalDataClient,
    ticker: str,
    from_date: date,
    to_date: date,
    config: Config,
    lookback_days: int = 120,
    feed: str = "iex",
) -> DailyHistoryResult:
    """Download a full daily history and emit one cached parquet per trading day.

    Fetches a single Alpaca daily-bars request covering
    ``[from_date - buffer, to_date]`` and, for every trading day ``D`` in
    ``[from_date, to_date]``, writes ``<TICKER>/<D>_daily.parquet`` containing
    the last ``lookback_days`` trading-day bars ending on ``D``. Days that
    already have a cached file are skipped (idempotent).

    Returns a :class:`DailyHistoryResult` so callers can update progress
    counters, rate-limit windows, and surface coverage gaps.
    """
    trading_days: list[date] = _get_trading_days(from_date, to_date, config)
    if not trading_days:
        return DailyHistoryResult(0, 0, 0, 0)

    targets: list[date] = [
        d for d in trading_days if load_cached(ticker, d, "daily") is None
    ]
    skipped: int = len(trading_days) - len(targets)
    if not targets:
        logger.debug(
            "All %d daily files already cached for %s", len(trading_days), ticker,
        )
        return DailyHistoryResult(0, skipped, 0, 0)

    # Fetch enough calendar days to cover ``lookback_days`` trading days plus
    # holidays/weekends. ``lookback_days * 1.5 + 30`` is generous; the cost is
    # one extra Alpaca request slice, not per-day requests.
    buffer_days: int = int(lookback_days * 1.5) + 30
    request_start: datetime = datetime(
        from_date.year, from_date.month, from_date.day, tzinfo=TZ_UTC,
    ) - timedelta(days=buffer_days)
    request_end: datetime = datetime(
        to_date.year, to_date.month, to_date.day,
        23, 59, 59, tzinfo=TZ_UTC,
    )

    # Narrow try/except around the API call so transient network/auth errors
    # are absorbed but structural surprises (e.g. response shape mismatch)
    # surface to the caller instead of being conflated with "0 bars".
    try:
        from alpaca.data.enums import DataFeed, Adjustment
        feed_enum = DataFeed.IEX if feed.lower() == "iex" else DataFeed.SIP

        request = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=TimeFrame(1, TimeFrameUnit.Day),
            start=request_start,
            end=request_end,
            feed=feed_enum,
            adjustment=Adjustment.ALL,
        )
        response = client.get_stock_bars(request)
    except Exception:
        logger.exception(
            "Failed to download daily history for %s (%s to %s)",
            ticker, request_start.date(), request_end.date(),
        )
        return DailyHistoryResult(0, skipped, len(targets), 1)

    # The Alpaca SDK return type is union'd with ``dict[str, Any]`` (hence the
    # standing mypy ``union-attr`` warning at other call sites). A dict-shaped
    # response is a structural surprise — surface it rather than swallow it.
    raw_data: Any = getattr(response, "data", None)
    if raw_data is None:
        if isinstance(response, dict):
            raw_data = response
        else:
            logger.error(
                "Unexpected Alpaca response shape for %s: %s — treating as empty",
                ticker, type(response).__name__,
            )
            return DailyHistoryResult(0, skipped, len(targets), 1)
    bars: list = list(raw_data.get(ticker, []))

    if not bars:
        logger.warning(
            "Empty daily history for %s (%s to %s) — %d target days unwritten",
            ticker, request_start.date(), request_end.date(), len(targets),
        )
        return DailyHistoryResult(0, skipped, len(targets), 1)

    df_full: pd.DataFrame = _bars_to_df(bars)
    # Per-bar trading day, tz-normalised to UTC date for inclusive comparison.
    bar_dates = df_full.index.tz_convert(TZ_UTC).date

    written: int = 0
    missed: int = 0
    for d in targets:
        mask = bar_dates <= d
        df_slice: pd.DataFrame = df_full.loc[mask]
        if df_slice.empty:
            # Target predates Alpaca's history for this ticker — silent skip
            # here is exactly the failure mode this PR is meant to prevent.
            logger.warning(
                "No daily bars at-or-before %s for %s — file not written "
                "(check ticker history start vs from_date - buffer=%s)",
                d.isoformat(), ticker, request_start.date().isoformat(),
            )
            missed += 1
            continue
        if len(df_slice) < lookback_days:
            logger.debug(
                "Truncated lookback for %s @ %s: %d/%d bars (likely near "
                "Alpaca data-availability boundary)",
                ticker, d.isoformat(), len(df_slice), lookback_days,
            )
        if len(df_slice) > lookback_days:
            df_slice = df_slice.iloc[-lookback_days:]
        save_to_cache(ticker, d, "daily", df_slice)
        written += 1

    log = logger.warning if missed else logger.info
    log(
        "Daily history for %s: %d written, %d skipped (cached), "
        "%d missed (no coverage) — 1 API request",
        ticker, written, skipped, missed,
    )
    return DailyHistoryResult(written, skipped, missed, 1)


async def download_all(
    config: Config,
    tickers: list[str],
    from_date: date,
    to_date: date,
    rate_limit_per_min: int = 180,
    full_daily_history: bool = False,
    daily_lookback_days: int = 120,
) -> dict[str, int]:
    """Download intraday + daily bars for all tickers across the date range.

    Respects Alpaca's free-tier rate limit (~200 req/min).
    Returns a dict of status counts.

    When ``full_daily_history`` is True, daily bars are saved as one parquet
    per trading day across ``[from_date, to_date]`` (see
    :func:`download_daily_history`). Otherwise a single 120-day file is saved
    at the endpoint, matching the legacy single-tick consumption pattern.
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
    if full_daily_history:
        stats.update({"daily_written": 0, "daily_skipped": 0, "daily_missed": 0})
    request_count: int = 0
    window_start: float = time.monotonic()

    async def _respect_rate_limit() -> None:
        """Pause if we've sent ``rate_limit_per_min`` requests this window."""
        nonlocal request_count, window_start
        if request_count >= rate_limit_per_min:
            elapsed: float = time.monotonic() - window_start
            if elapsed < 60:
                wait: float = 60 - elapsed + 1
                logger.info("Rate limit: waiting %.0fs...", wait)
                await asyncio.sleep(wait)
            request_count = 0
            window_start = time.monotonic()

    for ticker in tickers:
        if full_daily_history:
            await _respect_rate_limit()
            result: DailyHistoryResult = await download_daily_history(
                client, ticker, from_date, to_date, config,
                lookback_days=daily_lookback_days, feed=feed,
            )
            stats["daily_written"] += result.written
            stats["daily_skipped"] += result.skipped
            stats["daily_missed"] += result.missed
            request_count += result.requests_made
        else:
            # Legacy single-file path: one daily parquet per ticker, anchored
            # on ``to_date`` with 120-day lookback. Sufficient for the live
            # bot's single-tick read.
            _, daily_status = await download_daily_bars(
                client, ticker, to_date, lookback_days=120, feed=feed,
            )
            stats[daily_status] = stats.get(daily_status, 0) + 1
            if daily_status == "downloaded":
                request_count += 1

        for day in trading_days:
            await _respect_rate_limit()
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

    if stats.get("daily_missed"):
        logger.warning(
            "Download complete with %d missed daily targets — cache may be "
            "incomplete; see prior warnings for affected (ticker, date) pairs.",
            stats["daily_missed"],
        )
    logger.info(
        "Download complete: cached=%d downloaded=%d empty=%d error=%d%s",
        stats["cached"], stats["downloaded"], stats["empty"], stats["error"],
        (
            f" daily_written={stats['daily_written']} "
            f"daily_skipped={stats['daily_skipped']} "
            f"daily_missed={stats['daily_missed']}"
            if full_daily_history else ""
        ),
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
    parser.add_argument(
        "--full-daily-history",
        action="store_true",
        help=(
            "Write one daily parquet per trading day across [from, to] "
            "(needed for multi-year backtests where the regime filter "
            "must calibrate against more than the last 120 days)."
        ),
    )
    parser.add_argument(
        "--daily-lookback",
        type=int,
        default=120,
        help=(
            "Lookback (trading days) baked into each per-day daily parquet "
            "in --full-daily-history mode. Default 120 is enough for the "
            "regime-filter 50-SMA. Strategies that need deeper history (e.g. "
            "cross_sectional_momentum with lookback_days=126, skip=21 = 148 "
            "rows) should pass --daily-lookback 200 or higher and "
            "delete the existing daily cache before re-running."
        ),
    )
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

    stats = await download_all(
        config, tickers, d_from, d_to,
        full_daily_history=args.full_daily_history,
        daily_lookback_days=args.daily_lookback,
    )
    print(f"\nDownload complete: {stats}")
    print(f"Log file: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())

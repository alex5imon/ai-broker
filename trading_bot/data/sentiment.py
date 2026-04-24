"""Finnhub sentiment integration.

Fetches company news sentiment from the Finnhub API, caches results in
SQLite, and provides normalized sentiment scores for individual tickers,
sectors, and the overall market.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import finnhub

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")


class _RateLimiter:
    """Simple token-bucket rate limiter for Finnhub API calls."""

    def __init__(self, max_calls: int, period_seconds: float) -> None:
        self._max_calls: int = max_calls
        self._period: float = period_seconds
        self._timestamps: list[float] = []
        self._lock: asyncio.Lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a call slot is available."""
        async with self._lock:
            now: float = time.monotonic()

            # Purge expired timestamps
            cutoff: float = now - self._period
            self._timestamps = [t for t in self._timestamps if t > cutoff]

            if len(self._timestamps) >= self._max_calls:
                # Must wait for the oldest call to expire
                wait_time: float = self._timestamps[0] - cutoff
                if wait_time > 0:
                    logger.debug("Rate limiter: waiting %.1fs", wait_time)
                    await asyncio.sleep(wait_time)

                # Re-purge after sleep
                now = time.monotonic()
                cutoff = now - self._period
                self._timestamps = [t for t in self._timestamps if t > cutoff]

            self._timestamps.append(time.monotonic())


class SentimentAnalyzer:
    """Fetches and caches stock sentiment from Finnhub.

    Uses the Finnhub news-sentiment endpoint to get company news scores,
    normalizes them to a -1.0 to +1.0 range, and caches in SQLite with
    a configurable TTL. Provides individual, sector, and market-level
    sentiment aggregations.
    """

    def __init__(self, config: dict[str, Any], db_path: str) -> None:
        self._config: dict[str, Any] = config
        self._db_path: str = db_path

        # API key from environment
        api_key: str | None = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            logger.warning(
                "FINNHUB_API_KEY not set. Sentiment queries will return None."
            )
            self._client: finnhub.Client | None = None
        else:
            self._client = finnhub.Client(api_key=api_key)

        # Config values
        sent_cfg: dict[str, Any] = config.get("sentiment", {})
        self._cache_ttl_minutes: int = int(
            sent_cfg.get("cache_ttl_minutes", 30)
        )
        rate_limit: int = int(
            sent_cfg.get("finnhub_rate_limit_per_minute", 60)
        )
        self._market_symbols: list[str] = list(
            sent_cfg.get("market_symbols", ["SPY", "QQQ"])
        )
        self._market_reduce_threshold: float = float(
            sent_cfg.get("market_reduce_threshold", -0.2)
        )
        self._market_close_only_threshold: float = float(
            sent_cfg.get("market_close_only_threshold", -0.4)
        )
        self._sector_block_threshold: float = float(
            sent_cfg.get("sector_block_threshold", -0.1)
        )

        # Rate limiter
        self._rate_limiter: _RateLimiter = _RateLimiter(
            max_calls=rate_limit, period_seconds=60.0
        )

        # Ensure DB table exists
        self._init_db()

    def _init_db(self) -> None:
        """Ensure the sentiment_cache table exists in the database."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sentiment_cache (
                        ticker      TEXT NOT NULL,
                        score       REAL NOT NULL,
                        raw_score   REAL,
                        source      TEXT NOT NULL,
                        timestamp   TEXT NOT NULL,
                        PRIMARY KEY (ticker, source)
                    )
                    """
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to initialize sentiment_cache table")

    # -------------------------------------------------------------------------
    # Core sentiment fetching
    # -------------------------------------------------------------------------

    async def get_sentiment(self, ticker: str) -> float | None:
        """Get sentiment score for a ticker.

        Checks SQLite cache first. If the cached value is fresh (within TTL),
        returns it directly. Otherwise fetches from Finnhub, caches, and
        returns the normalized score.

        Args:
            ticker: Stock symbol to query.

        Returns:
            Normalized sentiment score (-1.0 to +1.0), or None if
            no data is available.
        """
        # Check cache first
        cached: float | None = self._get_cached_score(ticker)
        if cached is not None:
            return cached

        # Fetch from Finnhub
        raw_score: float | None = await self._fetch_from_finnhub(ticker)
        if raw_score is None:
            return None

        normalized: float = self._normalize_score(raw_score)
        self._cache_score(ticker, normalized, raw_score, "finnhub_news")
        return normalized

    async def get_sector_sentiment(self, sector: str) -> float:
        """Average sentiment of all watchlist tickers in a GICS sector.

        Args:
            sector: GICS sector name (e.g., 'Financials', 'Energy').

        Returns:
            Average normalized sentiment for the sector, or 0.0 if no data.
        """
        # Collect all tickers that could be in this sector from the watchlist
        watchlist: dict[str, Any] = self._config.get("watchlist", {})
        all_tickers: list[str] = []
        for market_key in ("lse", "us"):
            all_tickers.extend(watchlist.get(market_key, []))

        scores: list[float] = []
        for ticker in all_tickers:
            score: float | None = await self.get_sentiment(ticker)
            if score is not None:
                scores.append(score)

        if not scores:
            logger.debug("No sentiment data for sector '%s'", sector)
            return 0.0

        avg: float = sum(scores) / len(scores)
        logger.debug(
            "Sector '%s' sentiment: %.3f (from %d tickers)",
            sector,
            avg,
            len(scores),
        )
        return avg

    async def get_market_sentiment(self) -> float:
        """Average sentiment of market benchmark symbols (SPY + QQQ).

        Returns:
            Average normalized sentiment for the overall market.
            Returns 0.0 if no data available.
        """
        scores: list[float] = []

        for symbol in self._market_symbols:
            score: float | None = await self.get_sentiment(symbol)
            if score is not None:
                scores.append(score)

        if not scores:
            logger.debug("No market sentiment data available")
            return 0.0

        avg: float = sum(scores) / len(scores)
        logger.info("Market sentiment: %.3f (from %s)", avg, self._market_symbols)
        return avg

    def is_market_close_only(self, market_sentiment: float) -> bool:
        """Check if market sentiment warrants close-only mode.

        Args:
            market_sentiment: Current market sentiment score.

        Returns:
            True if sentiment is below the close-only threshold.
        """
        return market_sentiment < self._market_close_only_threshold

    def is_market_reduced_size(self, market_sentiment: float) -> bool:
        """Check if market sentiment warrants reduced position sizes.

        Args:
            market_sentiment: Current market sentiment score.

        Returns:
            True if sentiment is below the reduce-size threshold.
        """
        return market_sentiment < self._market_reduce_threshold

    def is_sector_blocked(self, sector_sentiment: float) -> bool:
        """Check if a sector's sentiment blocks new entries.

        Args:
            sector_sentiment: The sector's average sentiment score.

        Returns:
            True if the sector sentiment is below the block threshold.
        """
        return sector_sentiment < self._sector_block_threshold

    # -------------------------------------------------------------------------
    # Bulk refresh
    # -------------------------------------------------------------------------

    async def refresh_all(self, tickers: list[str]) -> None:
        """Refresh sentiment for all tickers with rate limiting.

        Fetches sentiment for each ticker sequentially, respecting the
        Finnhub rate limit. Also refreshes market benchmark symbols.

        Args:
            tickers: List of stock symbols to refresh.
        """
        all_symbols: list[str] = list(tickers) + [
            s for s in self._market_symbols if s not in tickers
        ]

        logger.info("Refreshing sentiment for %d symbols", len(all_symbols))
        success_count: int = 0
        fail_count: int = 0

        for symbol in all_symbols:
            try:
                score: float | None = await self.get_sentiment(symbol)
                if score is not None:
                    success_count += 1
                else:
                    fail_count += 1
            except Exception:
                logger.exception("Error refreshing sentiment for %s", symbol)
                fail_count += 1

        logger.info(
            "Sentiment refresh complete: %d succeeded, %d failed",
            success_count,
            fail_count,
        )

    # -------------------------------------------------------------------------
    # Finnhub API interaction
    # -------------------------------------------------------------------------

    async def _fetch_from_finnhub(self, ticker: str) -> float | None:
        """Fetch raw news sentiment score from Finnhub.

        Respects rate limiting. Returns the companyNewsScore or None
        on failure.

        Args:
            ticker: Stock symbol to query.

        Returns:
            Raw Finnhub companyNewsScore (0 to 1), or None.
        """
        if self._client is None:
            logger.debug("No Finnhub client, returning None for %s", ticker)
            return None

        await self._rate_limiter.acquire()

        try:
            # Run the synchronous Finnhub call in a thread executor
            loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
            result: dict[str, Any] = await loop.run_in_executor(
                None, self._client.news_sentiment, ticker
            )
        except Exception as exc:
            # Handle rate limit (429) specifically
            exc_str: str = str(exc)
            if "429" in exc_str or "rate limit" in exc_str.lower():
                logger.warning(
                    "Finnhub rate limit hit for %s, using cached data", ticker
                )
            else:
                logger.warning(
                    "Finnhub API error for %s: %s", ticker, exc_str
                )
            return None

        if not result:
            logger.debug("Empty Finnhub response for %s", ticker)
            return None

        # Extract sentiment data
        sentiment_data: dict[str, Any] | None = result.get("sentiment")
        if sentiment_data is None:
            # Try top-level companyNewsScore
            score = result.get("companyNewsScore")
            if score is not None:
                return float(score)
            logger.debug("No sentiment data in Finnhub response for %s", ticker)
            return None

        company_score = sentiment_data.get("companyNewsScore")
        if company_score is None:
            # Try the buzz-based score
            company_score = result.get("companyNewsScore")

        if company_score is not None:
            return float(company_score)

        logger.debug("No companyNewsScore in Finnhub response for %s", ticker)
        return None

    # -------------------------------------------------------------------------
    # Normalization
    # -------------------------------------------------------------------------

    @staticmethod
    def _normalize_score(finnhub_score: float) -> float:
        """Normalize Finnhub score to -1.0 to +1.0 range.

        Finnhub companyNewsScore is between 0 and 1.
        Normalization formula: (raw_score - 0.5) * 2

        Args:
            finnhub_score: Raw score from Finnhub (0 to 1).

        Returns:
            Normalized score clamped to [-1.0, +1.0].
        """
        normalized: float = (finnhub_score - 0.5) * 2.0
        return max(-1.0, min(1.0, normalized))

    # -------------------------------------------------------------------------
    # SQLite cache operations
    # -------------------------------------------------------------------------

    def _get_cached_score(self, ticker: str) -> float | None:
        """Get cached sentiment score if it exists and is fresh.

        Args:
            ticker: Stock symbol.

        Returns:
            Cached normalized score, or None if stale/missing.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    """
                    SELECT score, timestamp
                    FROM sentiment_cache
                    WHERE ticker = ? AND source = 'finnhub_news'
                    """,
                    (ticker,),
                ).fetchone()
        except sqlite3.Error:
            logger.exception("Error reading sentiment cache for %s", ticker)
            return None

        if row is None:
            return None

        score: float = row[0]
        timestamp_str: str = row[1]

        try:
            cached_time: datetime = datetime.fromisoformat(timestamp_str)
            if cached_time.tzinfo is None:
                cached_time = cached_time.replace(tzinfo=ET)
        except (ValueError, TypeError):
            logger.warning("Invalid timestamp in sentiment cache for %s", ticker)
            return None

        now: datetime = datetime.now(tz=ET)
        age: timedelta = now - cached_time

        if age.total_seconds() > self._cache_ttl_minutes * 60:
            logger.debug(
                "Cached sentiment for %s is stale (%.0f min old, TTL=%d min)",
                ticker,
                age.total_seconds() / 60,
                self._cache_ttl_minutes,
            )
            return None

        logger.debug(
            "Using cached sentiment for %s: %.3f (%.0f min old)",
            ticker,
            score,
            age.total_seconds() / 60,
        )
        return score

    def _cache_score(
        self,
        ticker: str,
        normalized_score: float,
        raw_score: float | None,
        source: str,
    ) -> None:
        """Write a sentiment score to the SQLite cache.

        Uses INSERT OR REPLACE to upsert on the (ticker, source) primary key.

        Args:
            ticker: Stock symbol.
            normalized_score: The normalized score (-1.0 to +1.0).
            raw_score: Original Finnhub score (0 to 1), or None.
            source: Source identifier (e.g., 'finnhub_news').
        """
        now_str: str = datetime.now(tz=ET).isoformat()

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO sentiment_cache
                        (ticker, score, raw_score, source, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (ticker, normalized_score, raw_score, source, now_str),
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("Error writing sentiment cache for %s", ticker)

    def clear_cache(self) -> None:
        """Clear all cached sentiment data."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("DELETE FROM sentiment_cache")
                conn.commit()
            logger.info("Sentiment cache cleared")
        except sqlite3.Error:
            logger.exception("Error clearing sentiment cache")

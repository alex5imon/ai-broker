"""Earnings calendar and blackout management.

Fetches earnings dates from Finnhub, caches them in SQLite, and provides
blackout window checking to prevent trading around earnings announcements.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import finnhub

from trading_bot.constants import TZ_EASTERN

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


class EarningsCalendar:
    """Manages earnings calendar data for blackout enforcement.

    Fetches upcoming earnings dates from the Finnhub API, stores them in
    SQLite, and determines whether a ticker is within its 48-hour
    pre/post-earnings blackout window.
    """

    def __init__(self, config: dict[str, Any], db_path: str) -> None:
        self._config: dict[str, Any] = config
        self._db_path: str = db_path

        # API key from environment
        api_key: str | None = os.environ.get("FINNHUB_API_KEY")
        if not api_key:
            logger.warning(
                "FINNHUB_API_KEY not set. Earnings calendar will be unavailable."
            )
            self._client: finnhub.Client | None = None
        else:
            self._client = finnhub.Client(api_key=api_key)

        # Config values
        entry_cfg: dict[str, Any] = config.get("entry", {})
        self._blackout_hours: int = int(
            entry_cfg.get("earnings_blackout_hours", 48)
        )

        # Ensure DB table exists
        self._init_db()

    def _init_db(self) -> None:
        """Ensure the earnings_calendar table exists."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS earnings_calendar (
                        ticker          TEXT NOT NULL,
                        earnings_date   TEXT NOT NULL,
                        earnings_hour   TEXT,
                        fetched_at      TEXT NOT NULL,
                        PRIMARY KEY (ticker, earnings_date)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_earnings_date
                    ON earnings_calendar(earnings_date)
                    """
                )
                conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to initialize earnings_calendar table")

    # -------------------------------------------------------------------------
    # Fetching from Finnhub
    # -------------------------------------------------------------------------

    async def refresh(self, tickers: list[str]) -> None:
        """Fetch and cache earnings dates for all tickers from Finnhub.

        Queries the Finnhub earnings calendar for the next 7 days and
        stores results in SQLite. Should be called once daily during
        pre-market scan.

        Args:
            tickers: List of stock symbols to check for earnings.
        """
        if self._client is None:
            logger.warning("No Finnhub client available, skipping earnings refresh")
            return

        today: date = date.today()
        from_date: str = today.strftime("%Y-%m-%d")
        # Fetch 10 days ahead to cover the full blackout window
        to_date: str = (today + timedelta(days=10)).strftime("%Y-%m-%d")

        logger.info(
            "Refreshing earnings calendar for %d tickers (%s to %s)",
            len(tickers),
            from_date,
            to_date,
        )

        try:
            loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
            result: dict[str, Any] = await loop.run_in_executor(
                None,
                lambda: self._client.earnings_calendar(
                    _from=from_date, to=to_date, symbol=""
                ),
            )
        except Exception:
            logger.exception("Failed to fetch earnings calendar from Finnhub")
            return

        if not result:
            logger.warning("Empty response from Finnhub earnings calendar")
            return

        earnings_list: list[dict[str, Any]] = result.get("earningsCalendar", [])
        if not earnings_list:
            logger.debug("No earnings events returned from Finnhub")
            return

        # Build a set of tickers we care about (case-insensitive lookup)
        ticker_set: set[str] = {t.upper() for t in tickers}

        # Also look up with common suffixes removed for LSE tickers
        # (e.g., "BP." -> "BP", "RR." -> "RR")
        for t in tickers:
            if t.endswith("."):
                ticker_set.add(t[:-1].upper())

        now_str: str = datetime.now(tz=ET).isoformat()
        inserted: int = 0

        try:
            with sqlite3.connect(self._db_path) as conn:
                for event in earnings_list:
                    symbol: str | None = event.get("symbol")
                    if symbol is None:
                        continue

                    symbol_upper: str = symbol.upper()
                    if symbol_upper not in ticker_set:
                        continue

                    earnings_date_str: str | None = event.get("date")
                    if not earnings_date_str:
                        continue

                    # Determine earnings hour: 'bmo', 'amc', or None
                    hour_raw: str | None = event.get("hour")
                    earnings_hour: str | None = None
                    if hour_raw:
                        hour_lower: str = hour_raw.lower().strip()
                        if hour_lower in ("bmo", "before market open", "before_market_open"):
                            earnings_hour = "bmo"
                        elif hour_lower in ("amc", "after market close", "after_market_close"):
                            earnings_hour = "amc"

                    # Map the symbol back to our ticker format
                    # (Finnhub may return "BP" but we use "BP.")
                    matched_ticker: str = self._match_ticker(
                        symbol_upper, tickers
                    )

                    conn.execute(
                        """
                        INSERT OR REPLACE INTO earnings_calendar
                            (ticker, earnings_date, earnings_hour, fetched_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (matched_ticker, earnings_date_str, earnings_hour, now_str),
                    )
                    inserted += 1

                conn.commit()

        except sqlite3.Error:
            logger.exception("Error writing earnings calendar to database")
            return

        logger.info(
            "Earnings calendar updated: %d events cached from %d total",
            inserted,
            len(earnings_list),
        )

    @staticmethod
    def _match_ticker(finnhub_symbol: str, tickers: list[str]) -> str:
        """Match a Finnhub symbol back to our local ticker format.

        Handles LSE tickers like "BP." where Finnhub returns "BP".

        Args:
            finnhub_symbol: The symbol as returned by Finnhub (uppercase).
            tickers: Our watchlist tickers.

        Returns:
            The matched ticker from our list, or the Finnhub symbol if
            no match is found.
        """
        for t in tickers:
            if t.upper() == finnhub_symbol:
                return t
            # Handle dot-suffixed LSE tickers
            if t.upper().rstrip(".") == finnhub_symbol:
                return t

        return finnhub_symbol

    # -------------------------------------------------------------------------
    # Blackout checking
    # -------------------------------------------------------------------------

    def is_in_blackout(self, ticker: str, current_time: datetime) -> bool:
        """Check if a ticker is within its earnings blackout window.

        The blackout window extends the configured number of hours (default 48)
        both before and after the scheduled earnings date. For 'bmo' earnings,
        the reference time is market open on the earnings date. For 'amc',
        it is market close. If timing is unknown, the entire day is used.

        Args:
            ticker: Stock symbol to check.
            current_time: Current datetime (timezone-aware, US/Eastern).

        Returns:
            True if the ticker is in a blackout window.
        """
        earnings_rows: list[tuple[str, str | None]] = self._get_earnings_rows(ticker)
        if not earnings_rows:
            return False

        blackout_delta: timedelta = timedelta(hours=self._blackout_hours)

        for earnings_date_str, earnings_hour in earnings_rows:
            try:
                e_date: date = date.fromisoformat(earnings_date_str)
            except ValueError:
                logger.warning(
                    "Invalid earnings date '%s' for %s", earnings_date_str, ticker
                )
                continue

            # Determine the reference datetime for the earnings event
            reference_dt: datetime = self._earnings_reference_time(
                e_date, earnings_hour
            )

            window_start: datetime = reference_dt - blackout_delta
            window_end: datetime = reference_dt + blackout_delta

            if window_start <= current_time <= window_end:
                logger.info(
                    "Skipping %s - earnings blackout (reports on %s %s)",
                    ticker,
                    earnings_date_str,
                    earnings_hour or "unknown",
                )
                return True

        return False

    def get_next_earnings(self, ticker: str) -> date | None:
        """Get the next earnings date for a ticker.

        Args:
            ticker: Stock symbol.

        Returns:
            The next upcoming earnings date, or None if not found.
        """
        today: date = date.today()

        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    """
                    SELECT earnings_date
                    FROM earnings_calendar
                    WHERE ticker = ? AND earnings_date >= ?
                    ORDER BY earnings_date ASC
                    LIMIT 1
                    """,
                    (ticker, today.isoformat()),
                ).fetchone()
        except sqlite3.Error:
            logger.exception("Error querying next earnings for %s", ticker)
            return None

        if row is None:
            return None

        try:
            return date.fromisoformat(row[0])
        except ValueError:
            logger.warning("Invalid earnings date in DB: '%s'", row[0])
            return None

    def get_blackout_tickers(
        self, tickers: list[str], current_time: datetime
    ) -> list[str]:
        """Get all tickers currently in earnings blackout.

        Args:
            tickers: List of symbols to check.
            current_time: Current datetime (timezone-aware).

        Returns:
            List of tickers that are currently in their blackout window.
        """
        return [
            t for t in tickers if self.is_in_blackout(t, current_time)
        ]

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _get_earnings_rows(self, ticker: str) -> list[tuple[str, str | None]]:
        """Fetch all earnings records for a ticker from the DB.

        Returns a list of (earnings_date, earnings_hour) tuples.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT earnings_date, earnings_hour
                    FROM earnings_calendar
                    WHERE ticker = ?
                    ORDER BY earnings_date ASC
                    """,
                    (ticker,),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("Error reading earnings calendar for %s", ticker)
            return []

        return rows

    @staticmethod
    def _earnings_reference_time(
        earnings_date: date, earnings_hour: str | None
    ) -> datetime:
        """Compute the reference datetime for an earnings event.

        For 'bmo' (before market open): 09:30 ET on the earnings date.
        For 'amc' (after market close): 16:00 ET on the earnings date.
        For unknown timing: 12:00 ET (midday) on the earnings date.

        Args:
            earnings_date: The date of the earnings announcement.
            earnings_hour: 'bmo', 'amc', or None.

        Returns:
            Reference datetime in US/Eastern.
        """
        if earnings_hour == "bmo":
            return datetime(
                earnings_date.year,
                earnings_date.month,
                earnings_date.day,
                9,
                30,
                tzinfo=ET,
            )
        elif earnings_hour == "amc":
            return datetime(
                earnings_date.year,
                earnings_date.month,
                earnings_date.day,
                16,
                0,
                tzinfo=ET,
            )
        else:
            # Unknown timing - use midday as reference
            return datetime(
                earnings_date.year,
                earnings_date.month,
                earnings_date.day,
                12,
                0,
                tzinfo=ET,
            )

    def clear_old_entries(self, days_old: int = 30) -> None:
        """Remove earnings entries older than the specified number of days.

        Args:
            days_old: Remove entries older than this many days.
        """
        cutoff: str = (date.today() - timedelta(days=days_old)).isoformat()

        try:
            with sqlite3.connect(self._db_path) as conn:
                result = conn.execute(
                    "DELETE FROM earnings_calendar WHERE earnings_date < ?",
                    (cutoff,),
                )
                conn.commit()
                logger.info(
                    "Cleared %d old earnings calendar entries (before %s)",
                    result.rowcount,
                    cutoff,
                )
        except sqlite3.Error:
            logger.exception("Error clearing old earnings entries")

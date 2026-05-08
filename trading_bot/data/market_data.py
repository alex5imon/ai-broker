"""Market data via Alpaca REST API.

Refactored to REST-only for the tick model (Phase 2).  The WebSocket streaming
path (StockDataStream, _on_quote/_on_trade handlers, staleness_monitor) has
been removed because GHA cron invocations are too short to maintain a stream.

Public methods that were async remain ``async def`` for caller compatibility;
their bodies are now synchronous.  A new ``refresh_quotes(tickers)`` method
bulk-fetches latest quotes via REST and updates the subscription cache; the
tick orchestrator should call it once per tick before strategy evaluation.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from trading_bot.constants import TZ_EASTERN

if TYPE_CHECKING:
    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


@dataclass
class MarketDataSubscription:
    """Cached market data for a ticker, populated by REST refresh_quotes()."""

    ticker: str
    exchange: str
    last_price: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: int = 0
    subscribed_at: datetime | None = None
    last_tick_time: datetime | None = None
    is_stale: bool = False
    excluded: bool = False


class MarketDataManager:
    """REST-only market data access for the tick-model bot.

    Maintains an in-memory subscription cache so strategy code can keep using
    ``get_latest_price`` / ``get_bid_ask`` / ``get_spread_pct``.  Populate the
    cache with ``refresh_quotes(tickers)`` once per tick.  Historical bars are
    fetched on demand via ``get_historical_bars``.
    """

    def __init__(
        self,
        gateway: GatewayConnection,
        config: dict[str, Any],
        notifier: Notifier,
    ) -> None:
        self._gateway: GatewayConnection = gateway
        self._config: dict[str, Any] = config
        self._notifier: Notifier = notifier

        self._subscriptions: dict[str, MarketDataSubscription] = {}

        md_cfg: dict[str, Any] = config.get("market_data", {})
        self._staleness_threshold_s: int = int(
            md_cfg.get("staleness_threshold_seconds", 30)
        )
        self._pause_on_staleness: bool = bool(
            md_cfg.get("pause_on_mass_staleness", True)
        )

        alpaca_cfg: dict[str, Any] = config.get("alpaca", {})
        self._data_feed: str = alpaca_cfg.get("data_feed", "iex")

        api_key: str = os.environ.get("ALPACA_API_KEY", "")
        secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")

        self._historical_client: StockHistoricalDataClient = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
        )

        self._trading_paused: bool = False
        self._running: bool = False

    # -------------------------------------------------------------------------
    # Subscribe / unsubscribe — register cache entries only (no WebSocket).
    # -------------------------------------------------------------------------

    async def subscribe(self, ticker: str, exchange: str = "US") -> None:
        """Register a cache entry for *ticker* and seed it with a REST quote."""
        if ticker in self._subscriptions and not self._subscriptions[ticker].excluded:
            logger.debug("Already subscribed to %s, skipping", ticker)
            return

        now: datetime = datetime.now(tz=ET)
        sub = MarketDataSubscription(
            ticker=ticker,
            exchange=exchange,
            subscribed_at=now,
            last_tick_time=now,
        )
        self._subscriptions[ticker] = sub

        try:
            request = StockLatestQuoteRequest(
                symbol_or_symbols=ticker, feed=self._data_feed
            )
            quotes = self._historical_client.get_stock_latest_quote(request)
            if ticker in quotes:
                q = quotes[ticker]
                sub.bid = float(q.bid_price) if q.bid_price else None
                sub.ask = float(q.ask_price) if q.ask_price else None
                if sub.bid and sub.ask:
                    sub.last_price = (sub.bid + sub.ask) / 2.0
        except Exception:
            # Benign: free IEX paper often returns no quote before market open.
            logger.debug("Initial quote fetch failed for %s", ticker, exc_info=True)

        logger.debug("Subscribed to market data for %s", ticker)

    async def unsubscribe(self, ticker: str) -> None:
        """Drop the cache entry for *ticker*."""
        if ticker in self._subscriptions:
            del self._subscriptions[ticker]
            logger.debug("Unsubscribed from market data for %s", ticker)

    async def unsubscribe_all(self) -> None:
        """Drop all cache entries."""
        self._subscriptions.clear()

    # -------------------------------------------------------------------------
    # REST quote refresh — the tick-model replacement for WebSocket streaming.
    # -------------------------------------------------------------------------

    async def refresh_quotes(self, tickers: list[str]) -> None:
        """Bulk-fetch latest quotes via REST and update the subscription cache.

        Subscriptions are created on the fly for any ticker not already tracked.
        The per-ticker ``last_tick_time`` is set to *now* on success.
        """
        if not tickers:
            return

        now: datetime = datetime.now(tz=ET)
        try:
            request = StockLatestQuoteRequest(
                symbol_or_symbols=tickers, feed=self._data_feed
            )
            quotes = self._historical_client.get_stock_latest_quote(request)
        except Exception:
            logger.exception("Bulk quote refresh failed for %d tickers", len(tickers))
            return

        for ticker in tickers:
            sub = self._subscriptions.get(ticker)
            if sub is None:
                sub = MarketDataSubscription(
                    ticker=ticker,
                    exchange="US",
                    subscribed_at=now,
                )
                self._subscriptions[ticker] = sub

            q = quotes.get(ticker)
            if q is None:
                continue
            sub.bid = float(q.bid_price) if q.bid_price else sub.bid
            sub.ask = float(q.ask_price) if q.ask_price else sub.ask
            if sub.bid and sub.ask:
                sub.last_price = (sub.bid + sub.ask) / 2.0
            sub.last_tick_time = now
            if sub.is_stale:
                logger.info("Market data recovered for %s", ticker)
                sub.is_stale = False
                sub.excluded = False

    # -------------------------------------------------------------------------
    # Price / quote accessors
    # -------------------------------------------------------------------------

    def get_latest_price(self, ticker: str) -> float | None:
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None
        return sub.last_price

    def get_bid_ask(self, ticker: str) -> tuple[float, float] | None:
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None
        if sub.bid is not None and sub.ask is not None and sub.bid > 0 and sub.ask > 0:
            return (sub.bid, sub.ask)
        return None

    def get_spread_pct(self, ticker: str) -> float | None:
        ba: tuple[float, float] | None = self.get_bid_ask(ticker)
        if ba is None:
            return None
        bid, ask = ba
        mid: float = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return (ask - bid) / mid

    def get_volume(self, ticker: str) -> int | None:
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None
        return sub.volume if sub.volume > 0 else None

    def get_ticker_object(self, ticker: str) -> MarketDataSubscription | None:
        return self._subscriptions.get(ticker)

    # -------------------------------------------------------------------------
    # Historical data
    # -------------------------------------------------------------------------

    async def get_historical_bars(
        self,
        ticker: str,
        exchange: str = "US",
        bar_size: str = "1 min",
        duration: str = "1 D",
    ) -> list[dict[str, Any]]:
        """Fetch historical bars from Alpaca (synchronous under the async wrapper)."""
        timeframe: TimeFrame = self._parse_bar_size(bar_size)
        start: datetime = self._parse_duration(duration)

        try:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=timeframe,
                start=start,
                feed=self._data_feed,
            )
            bars_response = self._historical_client.get_stock_bars(request)
            bars_data = bars_response.data.get(ticker, [])
        except Exception:
            logger.exception(
                "Failed to fetch historical bars for %s (%s, %s)",
                ticker, bar_size, duration,
            )
            return []

        if not bars_data:
            logger.warning("No historical data returned for %s", ticker)
            return []

        result: list[dict[str, Any]] = []
        for bar in bars_data:
            result.append({
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
                "date": bar.timestamp,
            })

        logger.debug(
            "Received %d historical bars for %s (%s, %s)",
            len(result), ticker, bar_size, duration,
        )
        return result

    @staticmethod
    def _parse_bar_size(bar_size: str) -> TimeFrame:
        bar_size_lower: str = bar_size.lower().strip()
        if bar_size_lower in ("1 min", "1min"):
            return TimeFrame(1, TimeFrameUnit.Minute)
        if bar_size_lower in ("5 mins", "5min", "5 min"):
            return TimeFrame(5, TimeFrameUnit.Minute)
        if bar_size_lower in ("15 mins", "15min", "15 min"):
            return TimeFrame(15, TimeFrameUnit.Minute)
        if bar_size_lower in ("1 hour", "1h"):
            return TimeFrame(1, TimeFrameUnit.Hour)
        if bar_size_lower in ("1 day", "1d"):
            return TimeFrame(1, TimeFrameUnit.Day)
        return TimeFrame(5, TimeFrameUnit.Minute)

    @staticmethod
    def _parse_duration(duration: str) -> datetime:
        now: datetime = datetime.now(tz=ET)
        parts: list[str] = duration.strip().split()
        if len(parts) != 2:
            return now - timedelta(days=1)

        amount: int = int(parts[0])
        unit: str = parts[1].upper()

        if unit == "D":
            return now - timedelta(days=amount)
        if unit == "W":
            return now - timedelta(weeks=amount)
        if unit == "M":
            return now - timedelta(days=amount * 30)
        return now - timedelta(days=amount)

    # -------------------------------------------------------------------------
    # Staleness detection — retained as a per-tick check against last_tick_time.
    # The long-running staleness_monitor loop is gone (no process to run it).
    # -------------------------------------------------------------------------

    def is_stale(self, ticker: str) -> bool:
        if not self._pause_on_staleness:
            return False
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return False
        return sub.is_stale

    def get_stale_symbols(self) -> list[str]:
        return [
            sym for sym, sub in self._subscriptions.items()
            if sub.is_stale and not sub.excluded
        ]

    def get_excluded_symbols(self) -> list[str]:
        return [sym for sym, sub in self._subscriptions.items() if sub.excluded]

    @property
    def trading_paused(self) -> bool:
        return self._trading_paused

    async def staleness_monitor(self) -> None:
        """No-op: the tick model has no long-running monitor loop."""
        logger.debug("staleness_monitor is a no-op in the tick model")

    def stop_monitor(self) -> None:
        self._running = False

    async def _check_staleness(self) -> None:
        """Mark subscriptions as stale when their last_tick_time is too old.

        The mass-staleness circuit breaker sets ``trading_paused`` and sends a
        notification when more than ``mass_staleness_pct`` of subscriptions
        have gone stale.  In the tick model this is invoked at most once per
        tick by the orchestrator.
        """
        now: datetime = datetime.now(tz=ET)
        threshold: timedelta = timedelta(seconds=self._staleness_threshold_s)

        total_subscribed: int = 0
        stale_count: int = 0

        for ticker, sub in list(self._subscriptions.items()):
            if sub.excluded:
                continue

            total_subscribed += 1
            last_tick_time: datetime | None = sub.last_tick_time
            if last_tick_time is None:
                last_tick_time = sub.subscribed_at or now

            time_since_tick: timedelta = now - last_tick_time
            if time_since_tick > threshold:
                if not sub.is_stale:
                    sub.is_stale = True
                    logger.warning(
                        "Market data stale for %s (no tick for %.0fs)",
                        ticker, time_since_tick.total_seconds(),
                    )
                stale_count += 1

        if total_subscribed == 0:
            return

        stale_ratio: float = stale_count / total_subscribed
        md_cfg: dict[str, Any] = self._config.get("market_data", {})
        mass_pct: float = float(md_cfg.get("mass_staleness_pct", 0.50))
        resume_pct: float = float(md_cfg.get("mass_staleness_resume_pct", 0.25))

        if stale_ratio > mass_pct and not self._trading_paused:
            if self._pause_on_staleness:
                self._trading_paused = True
            logger.critical(
                "Mass staleness: %d/%d symbols stale (%.0f%%)%s",
                stale_count, total_subscribed, stale_ratio * 100,
                "" if self._pause_on_staleness
                else " (pause disabled - trading continues via REST bars)",
            )
            if not self._pause_on_staleness:
                return
            await self._notifier.send(
                title="CRITICAL: Mass Market Data Staleness",
                message=(
                    f"Market data stale for {stale_count}/{total_subscribed} "
                    f"symbols ({stale_ratio:.0%}). Trading paused."
                ),
                priority=5,
                tags=["warning", "market_data"],
            )

        elif self._trading_paused and stale_ratio < resume_pct:
            self._trading_paused = False
            logger.info(
                "Mass staleness resolved: %d/%d (%.0f%%). Resuming.",
                stale_count, total_subscribed, stale_ratio * 100,
            )
            await self._notifier.send(
                title="Market Data Recovered",
                message=(
                    f"Stale count dropped to {stale_count}/{total_subscribed} "
                    f"({stale_ratio:.0%}). Trading resumed."
                ),
                priority=3,
                tags=["market_data"],
            )

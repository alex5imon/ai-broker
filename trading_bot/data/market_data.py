"""Real-time and historical market data management via Alpaca.

Handles real-time quote/trade subscriptions via Alpaca WebSocket,
staleness detection, and historical bar requests via Alpaca Data API.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.models import Bar, Quote, Trade
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.enums import DataFeed
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

if TYPE_CHECKING:
    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")

# Hard ceiling for a single synchronous Alpaca REST call. urllib3's internal
# retries can hang indefinitely on stale sockets after laptop sleep / network
# flap; this ensures an errant call cannot freeze the asyncio event loop.
REST_CALL_TIMEOUT_S: float = 30.0


@dataclass
class MarketDataSubscription:
    """Tracks a single market data subscription."""

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
    """Manages real-time market data via Alpaca WebSocket and REST API.

    Subscribes to real-time quotes for watchlist symbols, detects stale
    feeds, and serves historical bar requests for strategy calculations.
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
        # When false, mass staleness still logs but doesn't halt trading,
        # and is_stale() always reports False. See config.yaml for rationale.
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

        self._stream: StockDataStream = StockDataStream(
            api_key=api_key,
            secret_key=secret_key,
            feed=DataFeed(self._data_feed),
        )
        self._stream_task: asyncio.Task[None] | None = None
        self._stream_running: bool = False

        self._trading_paused: bool = False
        self._running: bool = False

    # -------------------------------------------------------------------------
    # Stream lifecycle
    # -------------------------------------------------------------------------

    async def start_stream(self) -> None:
        """Start the Alpaca WebSocket data stream in a background task.

        Per-symbol handlers are registered by subscribe() — do NOT call
        subscribe_quotes/subscribe_trades here without symbols, alpaca-py
        rejects that with ValueError.
        """
        if self._stream_running:
            return

        self._stream_running = True
        self._stream_task = asyncio.create_task(
            self._run_stream(), name="alpaca-data-stream"
        )
        logger.info("Alpaca data stream started (feed=%s)", self._data_feed)

    async def _run_stream(self) -> None:
        """Run the WebSocket stream.  Reconnects automatically on disconnect."""
        try:
            await self._stream._run_forever()
        except asyncio.CancelledError:
            logger.info("Data stream cancelled")
        except Exception:
            logger.exception("Data stream error — will restart on next subscribe")
            self._stream_running = False

    async def stop_stream(self) -> None:
        """Stop the data stream."""
        self._stream_running = False
        if self._stream_task is not None and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None
        try:
            await self._stream.close()
        except Exception:
            # Benign on shutdown: stream may already be half-closed.
            logger.debug("Error closing data stream", exc_info=True)

    # -------------------------------------------------------------------------
    # Quote / Trade callbacks
    # -------------------------------------------------------------------------

    async def _on_quote(self, quote: Quote) -> None:
        """Handle incoming quote from Alpaca WebSocket."""
        ticker: str = str(quote.symbol)
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return

        now: datetime = datetime.now(tz=ET)
        sub.bid = float(quote.bid_price) if quote.bid_price else sub.bid
        sub.ask = float(quote.ask_price) if quote.ask_price else sub.ask
        if sub.bid and sub.ask:
            sub.last_price = (sub.bid + sub.ask) / 2.0
        sub.last_tick_time = now

        if sub.is_stale:
            logger.info("Market data recovered for %s", ticker)
            sub.is_stale = False
            sub.excluded = False

    async def _on_trade(self, trade: Trade) -> None:
        """Handle incoming trade from Alpaca WebSocket."""
        ticker: str = str(trade.symbol)
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return

        now: datetime = datetime.now(tz=ET)
        sub.last_price = float(trade.price)
        sub.volume = int(trade.size) if trade.size else sub.volume
        sub.last_tick_time = now

        if sub.is_stale:
            logger.info("Market data recovered for %s", ticker)
            sub.is_stale = False
            sub.excluded = False

    # -------------------------------------------------------------------------
    # Subscribe / unsubscribe
    # -------------------------------------------------------------------------

    async def subscribe(self, ticker: str, exchange: str = "US") -> None:
        """Subscribe to real-time quotes for a ticker."""
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

        # Fetch initial quote via REST
        try:
            request = StockLatestQuoteRequest(symbol_or_symbols=ticker, feed=self._data_feed)
            quotes = await asyncio.wait_for(
                asyncio.to_thread(self._historical_client.get_stock_latest_quote, request),
                timeout=REST_CALL_TIMEOUT_S,
            )
            if ticker in quotes:
                q = quotes[ticker]
                sub.bid = float(q.bid_price) if q.bid_price else None
                sub.ask = float(q.ask_price) if q.ask_price else None
                if sub.bid and sub.ask:
                    sub.last_price = (sub.bid + sub.ask) / 2.0
        except Exception:
            # Benign: free IEX paper often returns no quote before market open.
            logger.debug("Initial quote fetch failed for %s", ticker, exc_info=True)

        # Subscribe via WebSocket.
        #
        # alpaca-py's subscribe_quotes/subscribe_trades take a fast path while
        # the stream isn't connected (just storing handlers) but once the WS
        # is connected they call asyncio.run_coroutine_threadsafe(...).result()
        # — which deadlocks if invoked from the same event loop that is
        # running the stream. Dispatch to a worker thread so .result() can
        # wait while our event loop continues to drive the stream.
        try:
            await asyncio.to_thread(
                self._stream.subscribe_quotes, self._on_quote, ticker
            )
            await asyncio.to_thread(
                self._stream.subscribe_trades, self._on_trade, ticker
            )
        except Exception:
            logger.exception("WebSocket subscribe failed for %s", ticker)

        if not self._stream_running:
            await self.start_stream()

        logger.info("Subscribed to market data for %s", ticker)

    async def unsubscribe(self, ticker: str) -> None:
        """Unsubscribe from market data for a ticker."""
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return

        try:
            await asyncio.to_thread(self._stream.unsubscribe_quotes, ticker)
            await asyncio.to_thread(self._stream.unsubscribe_trades, ticker)
        except Exception:
            # Benign: subscription may already be gone or stream closed.
            logger.debug("Error unsubscribing from %s", ticker, exc_info=True)

        del self._subscriptions[ticker]
        logger.info("Unsubscribed from market data for %s", ticker)

    async def subscribe_watchlist(self, market: str) -> None:
        """Subscribe to all tickers in the watchlist for a given market."""
        watchlist_cfg: dict[str, Any] = self._config.get("watchlist", {})
        tickers: list[str] = list(watchlist_cfg.get(market.lower(), []))

        logger.info("Subscribing to %d %s watchlist tickers", len(tickers), market.upper())

        for ticker in tickers:
            await self.subscribe(ticker, "US")
            await asyncio.sleep(0.05)

    async def unsubscribe_all(self) -> None:
        """Unsubscribe from all active market data subscriptions."""
        tickers: list[str] = list(self._subscriptions.keys())
        for ticker in tickers:
            await self.unsubscribe(ticker)

    # -------------------------------------------------------------------------
    # Price / quote accessors
    # -------------------------------------------------------------------------

    def get_latest_price(self, ticker: str) -> float | None:
        """Get the last traded price for a ticker."""
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None
        return sub.last_price

    def get_bid_ask(self, ticker: str) -> tuple[float, float] | None:
        """Get current bid and ask prices."""
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None

        if sub.bid is not None and sub.ask is not None and sub.bid > 0 and sub.ask > 0:
            return (sub.bid, sub.ask)
        return None

    def get_spread_pct(self, ticker: str) -> float | None:
        """Get current bid-ask spread as a percentage of mid-price."""
        ba: tuple[float, float] | None = self.get_bid_ask(ticker)
        if ba is None:
            return None

        bid, ask = ba
        mid: float = (bid + ask) / 2.0
        if mid <= 0:
            return None
        return (ask - bid) / mid

    def get_volume(self, ticker: str) -> int | None:
        """Get today's cumulative volume for a ticker."""
        sub: MarketDataSubscription | None = self._subscriptions.get(ticker)
        if sub is None:
            return None
        return sub.volume if sub.volume > 0 else None

    def get_ticker_object(self, ticker: str) -> MarketDataSubscription | None:
        """Get the subscription object for a ticker."""
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
        what_to_show: str = "TRADES",
    ) -> list[dict[str, Any]]:
        """Request historical data bars from Alpaca.

        Returns a list of dicts with keys: open, high, low, close, volume, date.
        """
        timeframe: TimeFrame = self._parse_bar_size(bar_size)
        start: datetime = self._parse_duration(duration)

        try:
            request = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=timeframe,
                start=start,
                feed=self._data_feed,
            )
            bars_response = await asyncio.wait_for(
                asyncio.to_thread(self._historical_client.get_stock_bars, request),
                timeout=REST_CALL_TIMEOUT_S,
            )
            bars_data = bars_response.data.get(ticker, [])
        except asyncio.TimeoutError:
            logger.error(
                "Historical bars timeout (>%.0fs) for %s (%s, %s)",
                REST_CALL_TIMEOUT_S, ticker, bar_size, duration,
            )
            return []
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
        """Convert IB-style bar size string to Alpaca TimeFrame."""
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
        """Convert IB-style duration string to a start datetime."""
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
    # Staleness detection
    # -------------------------------------------------------------------------

    def is_stale(self, ticker: str) -> bool:
        # When streaming-based pauses are disabled, strategies rely on REST
        # bar fetches for freshness, so always report not-stale here.
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
        """Background loop checking for stale market data."""
        self._running = True
        logger.info("Staleness monitor started")

        while self._running:
            try:
                await asyncio.sleep(5.0)
                await self._check_staleness()
            except asyncio.CancelledError:
                logger.info("Staleness monitor cancelled")
                break
            except Exception:
                logger.exception("Error in staleness monitor loop")
                await asyncio.sleep(5.0)

        logger.info("Staleness monitor stopped")

    def stop_monitor(self) -> None:
        self._running = False

    async def _check_staleness(self) -> None:
        """Check all subscriptions for staleness."""
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

        if total_subscribed > 0:
            stale_ratio: float = stale_count / total_subscribed
            mass_pct: float = float(
                self._config.get("market_data", {}).get("mass_staleness_pct", 0.50)
            )
            resume_pct: float = float(
                self._config.get("market_data", {}).get("mass_staleness_resume_pct", 0.25)
            )

            if stale_ratio > mass_pct and not self._trading_paused:
                if self._pause_on_staleness:
                    self._trading_paused = True
                logger.critical(
                    "Mass staleness: %d/%d symbols stale (%.0f%%)%s",
                    stale_count, total_subscribed, stale_ratio * 100,
                    "" if self._pause_on_staleness else " (pause disabled — trading continues via REST bars)",
                )
                if not self._pause_on_staleness:
                    # Don't send a pause notification or fall into the pause branch.
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

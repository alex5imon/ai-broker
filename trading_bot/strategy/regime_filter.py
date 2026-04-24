"""Market regime filter — blocks new entries in bearish regimes.

Matches the backtester's "SPY above 50-day SMA" gate that's applied in
``multi_strategy_backtest.run_daily()`` / ``run_spy_intraday()``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.strategy.technical import TechnicalAnalyzer

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")


class RegimeFilter:
    """Gate that returns False (block) when the market is in a bearish regime.

    Uses the index symbol (default SPY) on a daily bar series: if the current
    close is below the N-day SMA, new entries are blocked. The result is cached
    for ``cache_ttl_minutes`` to avoid re-fetching on every watchlist loop.
    """

    def __init__(
        self,
        get_daily_bars: Callable[[str, str], Awaitable[pd.DataFrame | None]],
        index_symbol: str = "SPY",
        sma_period: int = 50,
        enabled: bool = True,
        cache_ttl_minutes: int = 30,
    ) -> None:
        self._get_daily_bars = get_daily_bars
        self._index_symbol: str = index_symbol
        self._sma_period: int = sma_period
        self._enabled: bool = enabled
        self._cache_ttl: timedelta = timedelta(minutes=cache_ttl_minutes)
        self._cached_allowed: bool | None = None
        self._cached_at: datetime | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def allows_new_entries(self) -> bool:
        """True when the market regime is bullish (or filter disabled/unknown)."""
        if not self._enabled:
            return True

        now: datetime = datetime.now(tz=ET)
        if (
            self._cached_allowed is not None
            and self._cached_at is not None
            and now - self._cached_at < self._cache_ttl
        ):
            return self._cached_allowed

        try:
            df: pd.DataFrame | None = await self._get_daily_bars(self._index_symbol, "US")
        except Exception:
            logger.warning(
                "RegimeFilter: error fetching %s daily bars", self._index_symbol,
                exc_info=True,
            )
            return True  # Fail-open: don't block on transient data errors

        if df is None or df.empty or len(df) < self._sma_period + 1:
            logger.debug(
                "RegimeFilter: insufficient %s history (%d bars, need %d) — allowing",
                self._index_symbol, 0 if df is None else len(df), self._sma_period + 1,
            )
            return True

        sma: pd.Series = TechnicalAnalyzer.compute_sma(df, self._sma_period)
        if sma.isna().iloc[-1]:
            return True

        close_col: str = "close" if "close" in df.columns else "Close"
        current_price: float = float(df[close_col].iloc[-1])
        current_sma: float = float(sma.iloc[-1])
        allowed: bool = current_price > current_sma

        self._cached_allowed = allowed
        self._cached_at = now

        logger.info(
            "RegimeFilter: %s close=%.2f SMA%d=%.2f → new_entries=%s",
            self._index_symbol, current_price, self._sma_period, current_sma, allowed,
        )
        return allowed

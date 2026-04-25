"""Trend Following strategy — ride momentum with trailing stops."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading_bot.constants import HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.technical import TechnicalAnalyzer
from trading_bot.utils import coalesce

logger: logging.Logger = logging.getLogger(__name__)


class TrendFollowingStrategy(StrategyBase):
    """Enter on EMA crossover with trend confirmation; exit via trailing stop."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(
            strategy_id="trend_following",
            display_name="Trend Following",
            config=config,
        )
        self._sma_period: int = int(config.get("sma_period", 50))
        self._ema_fast: int = int(config.get("ema_fast", 9))
        self._ema_slow: int = int(config.get("ema_slow", 21))
        self._volume_multiplier: float = float(config.get("volume_multiplier", 1.5))
        self._trailing_stop_pct: float = float(config.get("trailing_stop_pct", 0.025))
        self._initial_stop_pct: float = float(config.get("initial_stop_pct", 0.03))
        self._max_positions: int = int(config.get("max_positions", 1))

    def evaluate_entry(
        self,
        ticker: str,
        exchange: str,
        df_5min: pd.DataFrame,
        df_daily: pd.DataFrame,
        current_price: float,
        available_cash: float,
        sentiment_score: float | None = None,
    ) -> StrategyDecision | None:
        # Need enough daily bars for SMA
        if len(df_daily) < self._sma_period + 5:
            return None
        if len(df_5min) < self._ema_slow + 5:
            return None

        # Trend filter: price above rising 50 SMA on daily
        sma50: pd.Series = TechnicalAnalyzer.compute_sma(df_daily, self._sma_period)
        if sma50.isna().iloc[-1]:
            return None
        current_sma: float = float(sma50.iloc[-1])
        prev_sma: float = float(sma50.iloc[-2]) if len(sma50) > 1 else current_sma

        if current_price <= current_sma:
            return None
        if current_sma < prev_sma:
            return None

        # EMA crossover on 5-min
        df_enriched: pd.DataFrame = df_5min.copy()
        df_enriched.columns = [c.lower() for c in df_enriched.columns]
        df_enriched["ema_fast"] = df_enriched["close"].ewm(span=self._ema_fast, adjust=False).mean()
        df_enriched["ema_slow"] = df_enriched["close"].ewm(span=self._ema_slow, adjust=False).mean()

        fast_now: float = float(df_enriched["ema_fast"].iloc[-1])
        slow_now: float = float(df_enriched["ema_slow"].iloc[-1])
        if fast_now <= slow_now:
            return None

        # Check crossover happened recently (last 3 bars)
        crossed: bool = False
        for i in range(-4, -1):
            if len(df_enriched) >= abs(i):
                if float(df_enriched["ema_fast"].iloc[i]) <= float(df_enriched["ema_slow"].iloc[i]):
                    crossed = True
                    break
        if not crossed:
            return None

        # Volume confirmation
        vol_avg: pd.Series = df_enriched["volume"].rolling(window=20).mean()
        current_vol: float = float(df_enriched["volume"].iloc[-1])
        avg_vol: float = float(vol_avg.iloc[-1]) if not vol_avg.isna().iloc[-1] else 0
        if avg_vol <= 0 or current_vol < self._volume_multiplier * avg_vol:
            return None

        stop_price: float = round(current_price * (1.0 - self._initial_stop_pct), 2)
        shares: int = self._compute_shares(current_price, stop_price, available_cash)
        if shares < 1:
            return None

        logger.info(
            "[%s] Trend following entry: %s price=$%.2f > SMA50=$%.2f, EMA cross confirmed, %d shares",
            self.strategy_id, ticker, current_price, current_sma, shares,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=shares,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=None,
            trail_pct=self._trailing_stop_pct,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={
                "sma50": current_sma,
                "ema_cross": True,
                "volume_ratio": current_vol / avg_vol if avg_vol > 0 else 0,
            },
            sentiment_score=sentiment_score,
        )

    def evaluate_exit(
        self,
        position: dict[str, Any],
        current_price: float,
        df_5min: pd.DataFrame | None = None,
        df_daily: pd.DataFrame | None = None,
    ) -> ExitSignal:
        entry_price: float = float(position.get("entry_price", 0))
        stop_price: float = float(coalesce(position, "stop_price", 0))
        highest_price: float = float(coalesce(position, "highest_price", entry_price))

        # Update highest price tracking
        if current_price > highest_price:
            highest_price = current_price

        # Initial stop loss
        if stop_price > 0 and current_price <= stop_price:
            return ExitSignal(should_exit=True, reason="stop_loss", is_emergency=True, use_market_order=True)

        # Trailing stop: once price has moved up, trail from highest
        if highest_price > entry_price:
            trail_stop: float = highest_price * (1.0 - self._trailing_stop_pct)
            if current_price <= trail_stop:
                return ExitSignal(should_exit=True, reason="trailing_stop")

        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

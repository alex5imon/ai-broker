"""Breakout strategy — buy new 20-day highs with volume."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading_bot.constants import HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.technical import TechnicalAnalyzer
from trading_bot.utils import coalesce

logger: logging.Logger = logging.getLogger(__name__)


class BreakoutStrategy(StrategyBase):
    """Enter on 20-day high breakout with volume; exit at 10-day low or stop."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            strategy_id="breakout",
            display_name="Breakout",
            config=config,
            **kwargs,
        )
        self._breakout_period: int = int(config.get("breakout_period", 20))
        self._exit_period: int = int(config.get("exit_period", 10))
        self._volume_multiplier: float = float(config.get("volume_multiplier", 1.5))
        self._stop_loss_pct: float = float(config.get("stop_loss_pct", 0.03))
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
        if len(df_daily) < self._breakout_period + 1:
            return None

        # Price must break above the 20-day high (excluding today)
        period_high: float = TechnicalAnalyzer.get_period_high(
            df_daily.iloc[:-1] if len(df_daily) > self._breakout_period else df_daily,
            self._breakout_period,
        )
        if current_price <= period_high:
            return None

        # Volume confirmation on 5-min bars
        if len(df_5min) < 21:
            return None
        # ``rename`` returns a new lightweight wrapper (no row copy) — the
        # caller's DataFrame is untouched, but we avoid a per-tick deep copy.
        df_e: pd.DataFrame = df_5min.rename(columns=str.lower)
        vol_avg: float = float(df_e["volume"].rolling(20).mean().iloc[-1])
        current_vol: float = float(df_e["volume"].iloc[-1])
        if vol_avg <= 0 or current_vol < self._volume_multiplier * vol_avg:
            return None

        stop_price: float = round(current_price * (1.0 - self._stop_loss_pct), 2)
        shares: int = self._compute_shares(current_price, stop_price, available_cash)
        if shares < 1:
            return None

        logger.info(
            "[%s] Breakout entry: %s price=$%.2f > %d-day high=$%.2f, %d shares",
            self.strategy_id, ticker, current_price, self._breakout_period, period_high, shares,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=shares,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=None,
            trail_pct=None,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={
                "breakout_high": period_high,
                "volume_ratio": current_vol / vol_avg if vol_avg > 0 else 0,
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
        stop_price: float = float(coalesce(position, "stop_price", 0))

        # Stop loss
        if stop_price > 0 and current_price <= stop_price:
            return ExitSignal(should_exit=True, reason="stop_loss", is_emergency=True, use_market_order=True)

        # Exit at 10-day low (Donchian exit)
        if df_daily is not None and len(df_daily) >= self._exit_period:
            period_low: float = TechnicalAnalyzer.get_period_low(df_daily, self._exit_period)
            if current_price <= period_low:
                return ExitSignal(should_exit=True, reason="period_low_exit")

        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

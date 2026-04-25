"""Sentiment Combo strategy — sentiment + any 1 technical signal."""

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading_bot.constants import HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.utils import coalesce

logger: logging.Logger = logging.getLogger(__name__)


class SentimentComboStrategy(StrategyBase):
    """Enter when Finnhub sentiment is positive AND at least one technical signal fires."""

    def __init__(self, config: dict[str, Any]) -> None:
        super().__init__(
            strategy_id="sentiment_combo",
            display_name="Sentiment Combo",
            config=config,
        )
        self._sentiment_threshold: float = float(config.get("sentiment_threshold", 0.15))
        self._min_technical_signals: int = int(config.get("min_technical_signals", 1))
        self._stop_loss_pct: float = float(config.get("stop_loss_pct", 0.015))
        self._take_profit_pct: float = float(config.get("take_profit_pct", 0.025))
        self._max_positions: int = int(config.get("max_positions", 2))

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
        # Require positive sentiment
        if sentiment_score is None or sentiment_score < self._sentiment_threshold:
            return None

        if len(df_5min) < 25:
            return None

        # Need at least 1 technical signal (EMA cross or BB bounce)
        df_e: pd.DataFrame = df_5min.copy()
        df_e.columns = [c.lower() for c in df_e.columns]
        df_e["ema_fast"] = df_e["close"].ewm(span=9, adjust=False).mean()
        df_e["ema_slow"] = df_e["close"].ewm(span=21, adjust=False).mean()

        bb_mid: pd.Series = df_e["close"].rolling(20).mean()
        bb_std: pd.Series = df_e["close"].rolling(20).std()
        df_e["bb_lower"] = bb_mid - 2.0 * bb_std
        df_e["bb_upper"] = bb_mid + 2.0 * bb_std

        tech_signals: int = 0

        # EMA cross check
        fast_now: float = float(df_e["ema_fast"].iloc[-1])
        slow_now: float = float(df_e["ema_slow"].iloc[-1])
        if fast_now > slow_now:
            for i in range(-4, -1):
                if len(df_e) >= abs(i):
                    if float(df_e["ema_fast"].iloc[i]) <= float(df_e["ema_slow"].iloc[i]):
                        tech_signals += 1
                        break

        # BB bounce check
        if not df_e["bb_lower"].isna().iloc[-1]:
            for i in range(-6, -1):
                if len(df_e) >= abs(i):
                    if float(df_e["low"].iloc[i]) <= float(df_e["bb_lower"].iloc[i]):
                        if float(df_e["close"].iloc[-1]) > float(df_e["close"].iloc[-2]):
                            tech_signals += 1
                            break

        if tech_signals < self._min_technical_signals:
            return None

        stop_price: float = round(current_price * (1.0 - self._stop_loss_pct), 2)
        target_price: float = round(current_price * (1.0 + self._take_profit_pct), 2)
        shares: int = self._compute_shares(current_price, stop_price, available_cash)
        if shares < 1:
            return None

        logger.info(
            "[%s] Sentiment combo entry: %s sentiment=%.2f, tech_signals=%d, %d shares @ $%.2f",
            self.strategy_id, ticker, sentiment_score, tech_signals, shares, current_price,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=shares,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_pct=None,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={
                "sentiment": sentiment_score,
                "tech_signal_count": tech_signals,
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

        # Stop loss
        if stop_price > 0 and current_price <= stop_price:
            return ExitSignal(should_exit=True, reason="stop_loss", is_emergency=True, use_market_order=True)

        # Take profit
        if entry_price > 0:
            target: float = entry_price * (1.0 + self._take_profit_pct)
            if current_price >= target:
                return ExitSignal(should_exit=True, reason="take_profit")

        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

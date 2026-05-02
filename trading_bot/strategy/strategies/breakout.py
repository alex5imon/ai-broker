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
        # Opt-in ATR-anchored stop. When enabled, replaces the fixed
        # stop_loss_pct with stop_atr_mult × ATR (typically 1.5×ATR for
        # asymmetric R/R breakouts). Defaults match the article-aligned
        # 1.5× stop.
        self._use_atr_stops: bool = bool(config.get("use_atr_stops", False))
        self._atr_period: int = int(config.get("atr_period", 14))
        self._atr_stop_mult: float = float(config.get("atr_stop_mult", 1.5))
        # H4 — per-ticker trend filter (price must be above its own SMA)
        self._require_trend_filter: bool = bool(config.get("require_trend_filter", False))
        self._trend_sma_period: int = int(config.get("trend_sma_period", 50))
        # H5 — ATR expansion filter (only enter when current ATR > avg ATR × mult)
        self._require_atr_expansion: bool = bool(config.get("require_atr_expansion", False))
        self._atr_expansion_lookback: int = int(config.get("atr_expansion_lookback", 20))
        self._atr_expansion_mult: float = float(config.get("atr_expansion_mult", 1.2))
        # H6 — pullback entry: don't buy the breakout bar; wait for retest of level
        self._pullback_entry: bool = bool(config.get("pullback_entry", False))
        self._pullback_window_bars: int = int(config.get("pullback_window_bars", 12))
        self._pullback_tolerance_pct: float = float(config.get("pullback_tolerance_pct", 0.005))

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

        # Volume confirmation on 5-min bars (computed up-front; used by both
        # immediate-entry and pullback-entry paths).
        if len(df_5min) < 21:
            return None
        df_e: pd.DataFrame = df_5min.rename(columns=str.lower)
        vol_avg: float = float(df_e["volume"].rolling(20).mean().iloc[-1])
        current_vol: float = float(df_e["volume"].iloc[-1])
        if vol_avg <= 0 or current_vol < self._volume_multiplier * vol_avg:
            return None

        # H6 — Pullback entry: wait for a retest of the breakout level
        # rather than buying the breakout bar itself. Diagnostic on
        # 2020-2026 13-ETF showed buying the breakout bar = top-ticking.
        if self._pullback_entry:
            window: int = max(self._pullback_window_bars, 2)
            if len(df_e) < window + 1:
                return None
            recent: pd.DataFrame = df_e.iloc[-window:]
            # 1) Did the high get broken in the last `window` bars?
            if float(recent["high"].max()) <= period_high:
                return None
            # 2) Has price pulled back to the breakout level?
            tol: float = self._pullback_tolerance_pct
            if current_price > period_high * (1.0 + tol):
                return None  # still extended, no pullback yet
            if current_price < period_high * (1.0 - tol):
                return None  # broke down through the level — failed breakout
            # 3) Current bar must show strength (close >= open)
            last_bar = df_e.iloc[-1]
            if float(last_bar["close"]) < float(last_bar["open"]):
                return None
        else:
            # Original logic: enter on the breakout bar itself.
            if current_price <= period_high:
                return None

        # H4 — Per-ticker trend filter: price must be above its own
        # SMA at the most recent fully-closed daily bar.
        if self._require_trend_filter:
            n: int = self._trend_sma_period
            if len(df_daily) < n + 1:
                return None
            sma: float = float(df_daily["close"].iloc[-(n + 1):-1].mean())
            if current_price <= sma:
                return None

        # H5 — ATR expansion filter: current ATR must be >= mult × the
        # mean ATR over the lookback window. Filters stale-range pops.
        if self._require_atr_expansion:
            current_atr: float | None = self._compute_atr(df_daily, self._atr_period)
            if current_atr is None:
                return None
            lookback: int = self._atr_expansion_lookback
            if len(df_daily) < self._atr_period + lookback + 1:
                return None
            atrs: list[float] = []
            for i in range(lookback):
                end: int = len(df_daily) - 1 - i
                window_df: pd.DataFrame = df_daily.iloc[max(0, end - self._atr_period):end]
                a: float | None = self._compute_atr(window_df, self._atr_period)
                if a is not None:
                    atrs.append(a)
            if len(atrs) < lookback // 2:
                return None
            avg_atr: float = float(sum(atrs) / len(atrs))
            if current_atr < self._atr_expansion_mult * avg_atr:
                return None

        if self._use_atr_stops:
            atr: float | None = self._compute_atr(df_daily, self._atr_period)
            if atr is not None and atr > 0:
                stop_price: float = round(
                    current_price - self._atr_stop_mult * atr, 2,
                )
            else:
                stop_price = round(current_price * (1.0 - self._stop_loss_pct), 2)
        else:
            stop_price = round(current_price * (1.0 - self._stop_loss_pct), 2)
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

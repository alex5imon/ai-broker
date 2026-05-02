"""Opening Range Breakout (ORB) strategy.

Defines the opening range from the first ``range_bars`` 5-minute bars
of the session (default 6 bars = 09:30-10:00 ET). Enters long on the
first 5-minute bar after the range closes that breaks above the range
high with a volume-confirmation check; stops at the range low; targets
``target_r_multiple`` × range above the breakout. INTRADAY hold type so
the backtester / live wind-down force-closes any unfilled exit at
session end.

Motivation: the prior breakout sleeve mixed a daily 20-day-high signal
with 5-minute execution and produced 0 wins on 13-ETF 2020-2026. Using
an intraday signal that already lives in the same timeframe as the
execution avoids the systematic top-tick pathology.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

import pandas as pd

from trading_bot.constants import TZ_EASTERN, HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.utils import coalesce

logger: logging.Logger = logging.getLogger(__name__)


class OpeningRangeBreakoutStrategy(StrategyBase):
    """Buy the breakout of the opening N-bar range; stop at range low."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            strategy_id="opening_range_breakout",
            display_name="Opening Range Breakout",
            config=config,
            **kwargs,
        )
        self._max_positions: int = int(config.get("max_positions", 3))
        # Opening range: first N 5-min bars. 6 = 09:30-10:00 ET.
        self._range_bars: int = int(config.get("range_bars", 6))
        # Entry must fire before this ET cutoff (skip late-day breakouts).
        self._entry_cutoff: time = _parse_time(
            str(config.get("entry_cutoff", "11:30"))
        )
        # Volume confirmation on the breakout bar.
        self._volume_multiplier: float = float(config.get("volume_multiplier", 1.3))
        # R-multiple target: target = orb_high + target_r × range.
        self._target_r_multiple: float = float(config.get("target_r_multiple", 1.0))
        # Per-trade equity risk for sizing — falls back through StrategyBase.
        self._risk_per_trade_pct: float = float(config.get("risk_per_trade_pct", 0.02))
        self._max_position_pct: float = float(config.get("max_position_pct", 0.33))
        self._fractional_shares: bool = bool(config.get("fractional_shares", True))
        # Skip entries when the opening range is < min_range_pct of price
        # — too tight = noise, false signal.
        self._min_range_pct: float = float(config.get("min_range_pct", 0.002))

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
        if df_5min is None or len(df_5min) < self._range_bars + 2:
            return None
        if available_cash <= 0 or current_price <= 0:
            return None

        df_e: pd.DataFrame = df_5min.rename(columns=str.lower)
        last_dt: datetime | None = _to_et_datetime(df_e.index[-1])
        if last_dt is None:
            return None

        # Time-of-day gates.
        bar_t: time = last_dt.time()
        if bar_t > self._entry_cutoff:
            return None

        today: date = last_dt.date()
        today_bars: pd.DataFrame = df_e[
            df_e.index.map(lambda ts: _to_et_date(ts) == today)
        ]
        if len(today_bars) < self._range_bars + 1:
            # Range not yet defined or no breakout bar after range.
            return None

        # Current bar must be after the range bars (range_bars + 1th onward).
        if today_bars.index[-1] != df_e.index[-1]:
            return None
        bars_so_far: int = len(today_bars)
        if bars_so_far <= self._range_bars:
            return None

        # Define opening range from the first range_bars bars of today.
        range_window: pd.DataFrame = today_bars.iloc[: self._range_bars]
        orb_high: float = float(range_window["high"].max())
        orb_low: float = float(range_window["low"].min())
        range_size: float = orb_high - orb_low
        if range_size <= 0:
            return None
        if range_size / current_price < self._min_range_pct:
            return None  # too-tight range, skip

        # Breakout: current price > range high.
        if current_price <= orb_high:
            return None

        # Volume confirmation on the breakout bar vs prior 20 bars
        # (or whatever's available — early-session bars use what we have).
        vol_window: pd.Series = df_e["volume"].iloc[-21:-1]
        if len(vol_window) < 5:
            return None
        vol_avg: float = float(vol_window.mean())
        current_vol: float = float(df_e["volume"].iloc[-1])
        if vol_avg <= 0 or current_vol < self._volume_multiplier * vol_avg:
            return None

        # Stops/targets anchored to the range.
        stop_price: float = round(orb_low, 4)
        target_price: float = round(
            orb_high + self._target_r_multiple * range_size, 4
        )

        # Position sizing — cap by max_position_pct of allocation, then
        # enforce risk_per_trade by trimming if stop distance is large.
        shares: float = self._size_shares(
            current_price, stop_price, available_cash,
        )
        if shares <= 0:
            return None

        logger.info(
            "[%s] ORB entry: %s @ $%.2f range=[$%.2f, $%.2f] "
            "tgt=$%.2f stop=$%.2f shares=%.4f vol_ratio=%.2f",
            self.strategy_id, ticker, current_price, orb_low, orb_high,
            target_price, stop_price, shares,
            (current_vol / vol_avg) if vol_avg else 0.0,
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
            hold_type=HoldType.INTRADAY,
            strategy_id=self.strategy_id,
            signals={
                "orb_high": orb_high,
                "orb_low": orb_low,
                "range_size_pct": range_size / current_price,
                "volume_ratio": (current_vol / vol_avg) if vol_avg else 0.0,
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
        target_price: float = float(coalesce(position, "target_price", 0))

        if stop_price > 0 and current_price <= stop_price:
            return ExitSignal(
                should_exit=True, reason="stop_loss",
                is_emergency=True, use_market_order=True,
            )
        if target_price > 0 and current_price >= target_price:
            return ExitSignal(should_exit=True, reason="take_profit")
        # Intraday wind-down handled by the backtester / live wind-down loop.
        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

    def _size_shares(
        self, entry_price: float, stop_price: float, available_cash: float,
    ) -> float:
        """Size by risk-per-trade, capped by max_position_pct."""
        if entry_price <= 0 or available_cash <= 0:
            return 0.0
        max_spend: float = available_cash * self._max_position_pct
        risk_dollars: float = available_cash * self._risk_per_trade_pct
        per_share_risk: float = max(entry_price - stop_price, 0.01)
        risk_shares: float = risk_dollars / per_share_risk
        cap_shares: float = max_spend / entry_price
        shares: float = min(risk_shares, cap_shares)
        if self._fractional_shares:
            return round(max(shares, 0.0), 4)
        return float(int(max(shares, 0.0)))


def _parse_time(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time string '{value}', expected HH:MM")
    return time(int(parts[0]), int(parts[1]))


def _to_et_datetime(ts: Any) -> datetime | None:
    if ts is None:
        return None
    dt = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        try:
            return dt.astimezone(TZ_EASTERN)
        except Exception:
            return dt
    return dt


def _to_et_date(ts: Any) -> date | None:
    dt = _to_et_datetime(ts)
    return dt.date() if dt is not None else None

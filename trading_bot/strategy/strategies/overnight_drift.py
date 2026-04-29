"""Overnight Drift strategy — buy at close, sell at next open.

Classic "overnight anomaly": historically, nearly all S&P 500 returns have
accrued overnight (close-to-open) rather than intraday. Simple and cheap
to implement; pairs with the intraday sleeves as a low-turnover
market-exposure baseline.

Entry rule : fire on the last 5-min bar of the session (within a
            configurable late-session window) when available. One
            position per ticker.
Exit rule  : close on the first bar of the next trading day.

Hold type is SWING so the backtester does NOT auto-close at wind-down.
The next-day-open exit is evaluated by this strategy's ``evaluate_exit``.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.constants import TZ_EASTERN, HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.utils import coalesce

logger: logging.Logger = logging.getLogger(__name__)


class OvernightDriftStrategy(StrategyBase):
    """Buy at close, sell at next open — capture the overnight risk premium."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            strategy_id="overnight_drift",
            display_name="Overnight Drift",
            config=config,
            **kwargs,
        )
        self._max_positions: int = int(config.get("max_positions", 1))
        # Entry window — last bar of the session. Default 15:45-15:55 ET
        # brackets the 15:50 wind-down of intraday sleeves while still
        # leaving room to fire once per day.
        entry_start_str: str = str(config.get("entry_window_start", "15:45"))
        entry_end_str: str = str(config.get("entry_window_end", "15:55"))
        self._entry_window_start: time = _parse_time(entry_start_str)
        self._entry_window_end: time = _parse_time(entry_end_str)
        # Disaster stop on the overnight leg. The anomaly has positive
        # expectancy on average but occasional large gap-downs occur
        # (earnings, macro). Set a hard -3% stop to cap tail risk.
        self._stop_loss_pct: float = float(config.get("stop_loss_pct", 0.03))
        self._fractional_shares: bool = bool(config.get("fractional_shares", True))
        # Deploy this fraction of available cash per entry. 0.95 leaves a
        # slim buffer for slippage.
        self._position_pct: float = float(config.get("position_pct", 0.95))

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
        if df_5min is None or len(df_5min) < 2:
            return None
        if available_cash <= 0 or current_price <= 0:
            return None

        bar_time: time | None = _last_bar_time(df_5min)
        if bar_time is None:
            return None

        # Only fire in the late-session window
        if not (self._entry_window_start <= bar_time <= self._entry_window_end):
            return None

        stop_price: float = round(current_price * (1.0 - self._stop_loss_pct), 2)

        # Slot-aware sizing — divide by ``max_positions`` so a $1,000
        # sleeve with 12 candidates doesn't blow $950 on the first
        # ticker and reject the next 11. ``position_pct`` retains its
        # role as the fraction of available cash to deploy *across*
        # all slots; per-slot share is ``position_pct / max_positions``.
        slots: int = max(1, self._max_positions)
        max_spend: float = (available_cash * self._position_pct) / slots
        if max_spend <= 0:
            return None
        shares: float = max_spend / current_price
        if self._fractional_shares:
            shares = round(shares, 4)
        else:
            shares = float(int(shares))

        min_shares: float = 0.001 if self._fractional_shares else 1.0
        if shares < min_shares:
            return None

        logger.info(
            "[%s] Overnight entry: %s @ $%.2f  shares=%.4f  stop=$%.2f",
            self.strategy_id, ticker, current_price, shares, stop_price,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=shares,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=None,       # no intraday target; exit on next open
            trail_pct=None,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={
                "entry_window": f"{self._entry_window_start}-{self._entry_window_end}",
                "hold": "overnight",
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

        # Disaster stop — redundant with the backtester's intrabar stop
        # check but belt-and-braces for the live path.
        if stop_price > 0 and current_price <= stop_price:
            return ExitSignal(
                should_exit=True,
                reason="stop_loss",
                is_emergency=True,
                use_market_order=True,
            )

        entry_date: date | None = _position_entry_date(position)
        if entry_date is None:
            return ExitSignal(should_exit=False)

        bar_date: date | None = _latest_bar_date(df_5min)
        if bar_date is None:
            # No bar data — fall back to "now" (live path may not pass bars).
            bar_date = datetime.now().date()

        # Exit on the first bar of any later trading day.
        if bar_date > entry_date:
            return ExitSignal(
                should_exit=True,
                reason="overnight_exit",
                is_emergency=False,
                use_market_order=True,
            )

        # Safety net — if something keeps the position open past 2 days
        # (weekend or unusual halt), force-close.
        if (bar_date - entry_date).days >= 2:
            return ExitSignal(
                should_exit=True,
                reason="overnight_timeout",
                is_emergency=True,
                use_market_order=True,
            )

        _ = entry_price  # silence unused-warning in linters
        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions


def _parse_time(value: str) -> time:
    """Parse 'HH:MM' into a ``time`` object. Raises on bad input."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time string '{value}', expected HH:MM")
    hour = int(parts[0])
    minute = int(parts[1])
    return time(hour, minute)


def _to_et_datetime(ts: Any) -> datetime | None:
    """Coerce a bar index value to a US/Eastern ``datetime``.

    Alpaca emits tz-aware UTC; the backtester emits ET-localized
    timestamps; some test fixtures emit naive datetimes. Convert tz-aware
    values to ET; treat naive as already-ET (backtest convention).
    """
    if ts is None:
        return None
    if hasattr(ts, "to_pydatetime"):
        dt = ts.to_pydatetime()
    else:
        dt = ts
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        try:
            return dt.astimezone(TZ_EASTERN)
        except Exception:
            return dt
    return dt


def _last_bar_time(df_5min: pd.DataFrame) -> time | None:
    """Return the wall-clock (ET) time-of-day of the last bar."""
    if df_5min is None or len(df_5min) == 0:
        return None
    dt = _to_et_datetime(df_5min.index[-1])
    return dt.time() if dt is not None else None


def _latest_bar_date(df_5min: pd.DataFrame | None) -> date | None:
    """Return the wall-clock (ET) date of the last bar."""
    if df_5min is None or len(df_5min) == 0:
        return None
    dt = _to_et_datetime(df_5min.index[-1])
    return dt.date() if dt is not None else None


def _position_entry_date(position: dict[str, Any]) -> date | None:
    """Extract entry date from a position dict.

    The backtester supplies ``entry_time`` as a ``datetime``; the live path
    reads positions from SQLite where ``entry_time`` is a TEXT column (ISO
    string). Handle both, plus a few reasonable fallback keys.
    """
    raw = position.get("entry_time")
    if raw is None:
        raw = position.get("entry_date")
    if raw is None:
        return None

    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        # datetime.fromisoformat handles most ISO 8601 shapes we'd emit,
        # including trailing "Z" via the replacement below.
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
        except ValueError:
            pass
        # Fall back to the date portion only.
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None

    return None

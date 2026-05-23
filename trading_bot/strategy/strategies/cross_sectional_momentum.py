"""Cross-sectional momentum strategy (sleeve #6, issue #44).

Ranks a universe of sector ETFs by trailing total return and holds the
top N on a monthly rebalance. Daily bars; monthly cadence; SWING hold
so the backtester's intraday wind-down does not flatten positions.

Cross-sectional: ranking requires daily bars for the full universe, not
just the per-ticker frame the framework passes into ``evaluate_entry``.
A ``universe_daily_loader`` callable is injected in the constructor and
serves the universe-wide data. The ranking is memoised per rebalance
date so the per-ticker call loop within one tick only loads the universe
once.

The strategy lands disabled. A regime-matched walkforward A/B must
clear the bar in issue #44 before ``enabled: true`` in config.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from typing import Any

import pandas as pd

from trading_bot.constants import TZ_EASTERN, HoldType
from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision

logger: logging.Logger = logging.getLogger(__name__)


UniverseDailyLoader = Callable[[str, date], "pd.DataFrame | None"]


def _default_universe_loader(ticker: str, as_of: date) -> pd.DataFrame | None:
    """Best-effort loader: reads the local daily parquet cache.

    Returns None on any error or cache miss. Callers should inject an
    explicit loader for production paths (live or backtest with a known
    data source); the default keeps the strategy importable from the
    registry without requiring extra wiring.
    """
    try:
        from trading_bot.data_cache import load_cached
        return load_cached(ticker, as_of, "daily")
    except Exception:  # noqa: BLE001
        return None


class CrossSectionalMomentumStrategy(StrategyBase):
    """Hold the top-N momentum names from a sector-ETF universe."""

    def __init__(
        self,
        config: dict[str, Any],
        universe_daily_loader: UniverseDailyLoader | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            strategy_id="cross_sectional_momentum",
            display_name="Cross-Sectional Momentum",
            config=config,
            **kwargs,
        )
        self._universe: tuple[str, ...] = tuple(config.get("universe", []))
        self._lookback_days: int = int(config.get("lookback_days", 126))
        self._skip_recent_days: int = int(config.get("skip_recent_days", 0))
        self._top_n: int = int(config.get("top_n", 3))
        self._max_positions: int = int(config.get("max_positions", self._top_n))
        self._rebalance_day_of_month: int = int(
            config.get("rebalance_day_of_month", 1)
        )
        self._rebalance_time: time = _parse_time(
            str(config.get("rebalance_time_et", "09:35"))
        )
        self._disaster_stop_pct: float = float(config.get("disaster_stop_pct", 0.15))
        self._fractional_shares: bool = bool(config.get("fractional_shares", True))
        self._position_pct: float = float(config.get("position_pct", 0.95))

        self._loader: UniverseDailyLoader = (
            universe_daily_loader or _default_universe_loader
        )
        self._calendar: HolidayCalendar = HolidayCalendar()

        # Memo: (as_of_date, top_n_set)
        self._ranking_memo: tuple[date, frozenset[str]] | None = None

    # ------------------------------------------------------------------
    # Entry
    # ------------------------------------------------------------------

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
        if ticker not in self._universe:
            return None
        if current_price <= 0 or available_cash <= 0:
            return None
        if df_5min is None or len(df_5min) == 0:
            return None

        bar_dt: datetime | None = _to_et_datetime(df_5min.index[-1])
        if bar_dt is None:
            return None
        today: date = bar_dt.date()

        if not self._is_rebalance_day(today):
            return None
        if bar_dt.time() < self._rebalance_time:
            return None

        top_n: frozenset[str] = self._top_n_for(today)
        if ticker not in top_n:
            return None

        slots: int = max(1, self._top_n)
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

        stop_price: float = round(current_price * (1.0 - self._disaster_stop_pct), 2)

        logger.info(
            "[%s] Entry: %s @ $%.2f shares=%.4f stop=$%.2f top_n=%s",
            self.strategy_id, ticker, current_price, shares, stop_price,
            sorted(top_n),
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
                "rebalance_date": today.isoformat(),
                "top_n": sorted(top_n),
                "lookback_days": self._lookback_days,
                "skip_recent_days": self._skip_recent_days,
            },
            sentiment_score=sentiment_score,
        )

    # ------------------------------------------------------------------
    # Exit
    # ------------------------------------------------------------------

    def evaluate_exit(
        self,
        position: dict[str, Any],
        current_price: float,
        df_5min: pd.DataFrame | None = None,
        df_daily: pd.DataFrame | None = None,
    ) -> ExitSignal:
        entry_price: float = float(position.get("entry_price", 0.0))

        # Disaster stop always wins, regardless of day-of-month.
        if entry_price > 0:
            loss_pct: float = (entry_price - current_price) / entry_price
            if loss_pct >= self._disaster_stop_pct:
                return ExitSignal(
                    should_exit=True,
                    reason="disaster_stop",
                    is_emergency=True,
                    use_market_order=True,
                )

        if df_5min is None or len(df_5min) == 0:
            return ExitSignal(should_exit=False)

        bar_dt: datetime | None = _to_et_datetime(df_5min.index[-1])
        if bar_dt is None:
            return ExitSignal(should_exit=False)
        today: date = bar_dt.date()
        if not self._is_rebalance_day(today):
            return ExitSignal(should_exit=False)
        if bar_dt.time() < self._rebalance_time:
            return ExitSignal(should_exit=False)

        ticker: str | None = position.get("ticker")
        if ticker is None or ticker not in self._universe:
            return ExitSignal(should_exit=False)

        top_n: frozenset[str] = self._top_n_for(today)
        if ticker in top_n:
            return ExitSignal(should_exit=False)

        return ExitSignal(
            should_exit=True,
            reason="rebalance_out",
            is_emergency=False,
            use_market_order=True,
        )

    def get_max_positions(self) -> int:
        return self._max_positions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_rebalance_day(self, today: date) -> bool:
        """True iff *today* is the first NYSE trading day of its month."""
        if not self._calendar.is_trading_day(today):
            return False
        probe: date = today - timedelta(days=1)
        while probe.month == today.month:
            if self._calendar.is_trading_day(probe):
                return False
            probe -= timedelta(days=1)
        return True

    def _top_n_for(self, today: date) -> frozenset[str]:
        if self._ranking_memo is not None and self._ranking_memo[0] == today:
            return self._ranking_memo[1]

        scores: list[tuple[str, float]] = []
        required_bars: int = self._lookback_days + self._skip_recent_days + 1
        for ticker in self._universe:
            df: pd.DataFrame | None = self._loader(ticker, today)
            if df is None or len(df) < required_bars:
                continue
            score: float | None = _lookback_total_return(
                df, lookback=self._lookback_days, skip_recent=self._skip_recent_days,
            )
            if score is None:
                continue
            scores.append((ticker, score))

        scores.sort(key=lambda kv: (-kv[1], kv[0]))  # higher return first, ticker tiebreak
        top: frozenset[str] = frozenset(t for t, _ in scores[: self._top_n])

        self._ranking_memo = (today, top)
        logger.debug(
            "[%s] Ranking for %s: %s (top %d of %d)",
            self.strategy_id, today.isoformat(),
            [(t, round(s, 4)) for t, s in scores],
            self._top_n, len(scores),
        )
        return top


# ---------------------------------------------------------------------------
# Free helpers
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> time:
    """Parse 'HH:MM' to a ``time``."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid time string '{value}', expected HH:MM")
    return time(int(parts[0]), int(parts[1]))


def _to_et_datetime(ts: Any) -> datetime | None:
    """Coerce a bar index value to a US/Eastern ``datetime``."""
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
        except Exception:  # noqa: BLE001
            return dt
    return dt


def _lookback_total_return(
    df: pd.DataFrame,
    lookback: int,
    skip_recent: int,
) -> float | None:
    """Total return over ``lookback`` bars ending ``skip_recent`` bars ago.

    With ``skip_recent=0`` this is just the trailing ``lookback`` return.
    With ``skip_recent>0`` the most recent ``skip_recent`` bars are
    excluded (the standard "skip-most-recent-month" momentum variant).

    Returns None when the frame is too short or the start price is
    non-positive.
    """
    if df is None or "close" not in df.columns:
        return None
    closes: pd.Series = df["close"].astype(float).dropna()
    needed: int = lookback + skip_recent + 1
    if len(closes) < needed:
        return None
    if skip_recent > 0:
        end_idx: int = -skip_recent - 1
    else:
        end_idx = -1
    start_idx: int = end_idx - lookback
    start_price: float = float(closes.iloc[start_idx])
    end_price: float = float(closes.iloc[end_idx])
    if start_price <= 0:
        return None
    return (end_price - start_price) / start_price

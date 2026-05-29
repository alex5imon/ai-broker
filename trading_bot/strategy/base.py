"""Abstract base class for trading strategies."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from trading_bot.constants import HoldType

logger: logging.Logger = logging.getLogger(__name__)


@dataclass
class StrategyDecision:
    """Entry decision produced by a strategy.

    Treat instances as immutable in transformation pipelines: helpers that
    need to adjust a decision (FOMC scaling, symbol-cap shrink) return a
    new instance via ``dataclasses.replace`` rather than mutating shared
    state.
    """

    ticker: str
    exchange: str
    direction: str  # "long"
    shares: float  # Accepts fractional shares (Alpaca supports down to 1/1000000)
    entry_price: float
    stop_price: float
    target_price: float | None  # None if using trailing only
    trail_pct: float | None  # None if using fixed target
    hold_type: HoldType
    strategy_id: str
    signals: dict[str, Any] = field(default_factory=dict)
    sentiment_score: float | None = None
    # Price at which an active trailing stop should replace the take-profit.
    # None means "activate trailing immediately on fill" (legacy behaviour).
    trail_activation_price: float | None = None


@dataclass
class ExitSignal:
    """Exit signal produced by a strategy."""

    should_exit: bool
    reason: str | None = None
    is_emergency: bool = False
    use_market_order: bool = False


class StrategyBase(ABC):
    """Abstract base for all trading strategies."""

    strategy_id: str
    display_name: str

    def __init__(
        self,
        strategy_id: str,
        display_name: str,
        config: dict[str, Any],
        db_path: str | None = None,
        vol_target_config: dict[str, Any] | None = None,
    ) -> None:
        self.strategy_id = strategy_id
        self.display_name = display_name
        self._config = config
        # Live vol-target context. Backtester passes None and uses its own
        # in-memory closed-trades buffer instead.
        self._db_path: str | None = db_path
        self._vol_target_config: dict[str, Any] = vol_target_config or {}
        # Optional per-strategy universe allow-list. When set, this sleeve
        # only trades tickers in the list — a guard against a sleeve being
        # run on a wider universe than it was validated on (e.g. ORB was
        # walkforward-validated on the 13-ETF basket, but at phase 3 the
        # shared watchlist expands to include individual mega-caps). Empty
        # / unset means "no restriction — trade the whole watchlist".
        raw_universe: Any = config.get("universe")
        # Named ``_universe_allowlist`` (not ``_universe``) to avoid colliding
        # with cross_sectional_momentum's own ``_universe`` ranking set.
        self._universe_allowlist: frozenset[str] | None = (
            frozenset(str(t).upper() for t in raw_universe)
            if isinstance(raw_universe, (list, tuple)) and raw_universe
            else None
        )

    def allows_ticker(self, ticker: str) -> bool:
        """Return True if this strategy may trade ``ticker``.

        Honours the optional per-strategy ``universe`` allow-list. With no
        allow-list configured, every ticker is allowed (legacy behaviour).
        """
        if self._universe_allowlist is None:
            return True
        return ticker.upper() in self._universe_allowlist

    @abstractmethod
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
        """Return a StrategyDecision if entry conditions met, else None."""

    @abstractmethod
    def evaluate_exit(
        self,
        position: dict[str, Any],
        current_price: float,
        df_5min: pd.DataFrame | None = None,
        df_daily: pd.DataFrame | None = None,
    ) -> ExitSignal:
        """Evaluate exit conditions for a position owned by this strategy."""

    @abstractmethod
    def get_max_positions(self) -> int:
        """Max concurrent positions for this strategy."""

    def _compute_shares(
        self,
        price: float,
        stop_price: float,
        available_cash: float,
        max_position_pct: float = 0.90,
    ) -> int:
        """Legacy cash-based share computation (fallback when risk sizing is off)."""
        max_spend: float = available_cash * max_position_pct
        if price <= 0:
            return 0
        shares: int = int(max_spend / price)
        return max(shares, 0)

    # ------------------------------------------------------------------
    # ATR + risk-based helpers (ported from multi_strategy_backtest)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 14) -> float | None:
        """Simple N-bar ATR on the last *period* bars. Returns None if insufficient data."""
        if df is None or len(df) < period + 1:
            return None

        d: pd.DataFrame = df.copy()
        d.columns = [c.lower() for c in d.columns]
        if not {"high", "low", "close"}.issubset(d.columns):
            return None

        high = d["high"].astype(float)
        low = d["low"].astype(float)
        close = d["close"].astype(float)
        prev_close = close.shift(1)
        tr = pd.concat(
            [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1)
        atr: float = float(tr.iloc[-period:].mean())
        return atr if atr > 0 else None

    @staticmethod
    def atr_adjusted_stops(
        entry_price: float,
        df: pd.DataFrame,
        atr_period: int = 14,
        stop_atr_mult: float = 2.0,
        target_atr_mult: float = 5.0,
        trail_atr_mult: float = 2.5,
        activation_atr_mult: float = 2.5,
        fallback_stop_pct: float = 0.02,
        fallback_target_pct: float = 0.05,
        fallback_trail_pct: float = 0.025,
        fallback_activation_pct: float = 0.025,
        min_stop_pct: float = 0.015,
        min_target_pct: float = 0.04,
        min_trail_pct: float = 0.02,
        min_activation_pct: float = 0.02,
    ) -> tuple[float, float, float, float]:
        """Compute (stop_price, target_price, trail_pct, activation_price) from ATR.

        Falls back to fixed percentages when ATR can't be computed.
        """
        atr: float | None = StrategyBase._compute_atr(df, atr_period)
        if atr is None or entry_price <= 0:
            stop_pct = fallback_stop_pct
            target_pct = fallback_target_pct
            trail_pct = fallback_trail_pct
            activation_pct = fallback_activation_pct
        else:
            stop_pct = max(stop_atr_mult * atr / entry_price, min_stop_pct)
            target_pct = max(target_atr_mult * atr / entry_price, min_target_pct)
            trail_pct = max(trail_atr_mult * atr / entry_price, min_trail_pct)
            activation_pct = max(activation_atr_mult * atr / entry_price, min_activation_pct)

        stop_price: float = round(entry_price * (1.0 - stop_pct), 2)
        target_price: float = round(entry_price * (1.0 + target_pct), 2)
        activation_price: float = round(entry_price * (1.0 + activation_pct), 2)
        return stop_price, target_price, trail_pct, activation_price

    @staticmethod
    def size_by_risk(
        entry_price: float,
        stop_price: float,
        available_cash: float,
        risk_per_trade_pct: float = 0.02,
        max_position_pct: float = 0.40,
        fractional: bool = False,
        vol_multiplier: float = 1.0,
    ) -> float:
        """Share count so max loss ≈ risk_per_trade_pct of available_cash.

        With ``fractional=True`` returns a fractional share count rounded to
        4 decimal places (Alpaca supports fractional shares down to 1/1000000).
        With ``fractional=False`` returns an integer share count.
        Bounded above by a max position value (max_position_pct of cash).
        ``vol_multiplier`` scales the risk budget (not the cash cap), so
        high-vol strategies shrink without breaking max-position-pct.
        Returns 0 when sizing is impossible.
        """
        if entry_price <= 0 or available_cash <= 0:
            return 0.0
        risk_per_share: float = abs(entry_price - stop_price)
        if risk_per_share <= 0:
            return 0.0

        risk_dollars: float = (
            available_cash * risk_per_trade_pct * max(vol_multiplier, 0.0)
        )
        shares_by_risk: float = risk_dollars / risk_per_share
        shares_by_cash: float = (available_cash * max_position_pct) / entry_price

        shares: float = max(min(shares_by_risk, shares_by_cash), 0.0)
        if fractional:
            return round(shares, 4)
        return float(int(shares))

    def vol_multiplier(self) -> float:
        """Live vol-target multiplier from this strategy's recent DB trades.

        Returns ``1.0`` (no adjustment) when:
          - db_path was not supplied (e.g. backtester),
          - the vol_target config is missing or annual_vol_pct ≤ 0,
          - or the trades table doesn't yet have ``min_sample`` rows for
            this strategy.

        Caller-side: pass the result as ``vol_multiplier=`` to
        :meth:`size_by_risk`. The result is cheap (one indexed query +
        a stdev), but still cache per tick if you call from a hot loop.
        """
        if not self._db_path:
            return 1.0
        target_annual: float = float(
            self._vol_target_config.get("annual_vol_pct", 0.0)
        )
        if target_annual <= 0:
            return 1.0

        from trading_bot.execution.vol_target import (
            load_recent_trade_returns,
            vol_target_multiplier,
        )

        lookback: int = int(self._vol_target_config.get("lookback_trades", 30))
        recent: list[float] = load_recent_trade_returns(
            self._db_path, self.strategy_id, lookback=lookback,
        )
        result = vol_target_multiplier(
            recent,
            target_annual_vol=target_annual,
            expected_trades_per_year=int(
                self._vol_target_config.get("trades_per_year", 252)
            ),
            min_multiplier=float(
                self._vol_target_config.get("min_multiplier", 0.5)
            ),
            max_multiplier=float(
                self._vol_target_config.get("max_multiplier", 1.5)
            ),
        )
        return result.multiplier

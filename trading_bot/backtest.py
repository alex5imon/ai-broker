"""Backtesting engine for the Alpaca trading bot.

Run via::

    python -m trading_bot.backtest --date 2026-04-15

Uses cached historical data (or fetches from Alpaca), walks 5-minute bars
chronologically, simulates fills with slippage, applies the full strategy
and exit logic, and produces a ``BacktestResult`` with per-trade detail
and aggregate metrics.

Alpaca is commission-free, so commission calculations are removed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.config import Config
from trading_bot.data_cache import load_cached
from trading_bot.constants import (
    GICS_SECTOR,
    TICKER_EXCHANGE,
    Exchange,
    HoldType,
    Market,
    TZ_EASTERN,
)
from trading_bot.db.repository import save_backtest_result
from trading_bot.strategy.technical import TechnicalAnalyzer

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class BacktestTrade:
    """Represents a single simulated trade within a backtest run."""

    ticker: str
    exchange: str
    currency: str
    entry_time: datetime
    entry_price: float          # Simulated fill (signal_price + slippage)
    signal_price: float         # Price when signal fired
    quantity: int
    hold_type: str              # 'intraday' or 'swing'
    signals: dict[str, Any]
    sentiment_score: float | None
    atr_rank: float
    stop_price: float
    target_price: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    highest_price: float | None = None
    trailing_active: bool = False
    commission: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    slippage_bps: float = 0.0


@dataclass
class BacktestResult:
    """Aggregate result of a single backtest run."""

    backtest_id: str
    run_date: str
    target_date: str
    initial_equity: float
    final_equity: float
    trades: list[BacktestTrade]
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    commissions: float = 0.0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    avg_hold_minutes: float = 0.0
    skipped_checks: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class BacktestEngine:
    """Uses cached historical data and simulates the strategy bar-by-bar.

    Parameters
    ----------
    config:
        Loaded ``Config`` instance.
    """

    _SLIPPAGE_BPS_DEFAULT: float = 2.0   # basis points per side

    def __init__(self, config: Config, *, use_cache: bool = True) -> None:
        self._config: Config = config
        self._use_cache: bool = use_cache
        self._analyzer: TechnicalAnalyzer = TechnicalAnalyzer(config)

        # Read backtesting config
        bt: dict[str, Any] = config._raw.get("backtesting", {})
        self._slippage_bps: float = float(bt.get("slippage_bps_per_side", self._SLIPPAGE_BPS_DEFAULT))
        self._default_sentiment: float = float(bt.get("default_sentiment", 0.0))

        # Read exit params (phase 1 is the default for backtests unless overridden)
        self._exit_intraday: dict[str, Any] = config.get_exit_params(HoldType.INTRADAY)
        self._exit_swing: dict[str, Any] = config.get_exit_params(HoldType.SWING)

        # ATR extreme threshold (skip entry if ATR rank >= this)
        self._atr_extreme: float = float(config._require("strategy", "atr", "extreme_percentile"))

        # Entry params
        self._min_signals: int = int(config._require("entry", "min_signals_required"))
        self._sentiment_threshold: float = float(config._require("entry", "sentiment_threshold"))
        self._sentiment_block: float = float(config._require("entry", "sentiment_block_threshold"))
        self._cooldown_minutes: int = int(config.entry_cooldown_minutes)

        # Diagnostic counters for backtest signal summary
        self._rejection_counts: dict[str, dict[str, int]] = {}
        self._signal_detail_counts: dict[str, dict[int, int]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        target_date: date,
        initial_equity: float = 950.0,
        phase: int = 1,
    ) -> BacktestResult:
        """Main entry point. Fetch data, walk bars, return result.

        Parameters
        ----------
        target_date:
            The trading day to simulate.
        initial_equity:
            Starting equity in GBP.
        phase:
            Phase (1, 2, 3) — controls position limits and risk params.
        """
        logger.info(
            "Starting backtest for %s | equity=%.2f | phase=%d",
            target_date.isoformat(),
            initial_equity,
            phase,
        )

        all_data: dict[str, dict[str, pd.DataFrame]] = await self._fetch_all_data(
            self._get_tickers(phase), target_date
        )

        if not all_data:
            logger.warning("No data fetched for %s — empty result", target_date)
            empty = BacktestResult(
                backtest_id=str(uuid.uuid4()),
                run_date=datetime.now(TZ_EASTERN).date().isoformat(),
                target_date=target_date.isoformat(),
                initial_equity=initial_equity,
                final_equity=initial_equity,
                trades=[],
                skipped_checks=["No market data available for this date"],
            )
            return empty

        result: BacktestResult = self._walk_bars(all_data, target_date, initial_equity, phase)
        result = self._calc_metrics(result)
        logger.info(
            "Backtest complete: %d trades | net_pnl=%.2f | win_rate=%.1f%%",
            result.total_trades,
            result.net_pnl,
            result.win_rate * 100,
        )
        return result

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    def _get_tickers(self, phase: int) -> list[str]:
        """Return the combined watchlist for the given phase."""
        raw: dict[str, Any] = self._config._raw.get("watchlist", {})
        tickers: list[str] = []
        tickers.extend(raw.get("lse", []))
        tickers.extend(raw.get("us", []))
        if phase >= 2:
            tickers.extend(raw.get("lse_phase2", []))
            tickers.extend(raw.get("us_phase2", []))
        if phase >= 3:
            tickers.extend(raw.get("us_phase3", []))
        return tickers

    async def _fetch_all_data(
        self,
        tickers: list[str],
        target_date: date,
    ) -> dict[str, dict[str, pd.DataFrame]]:
        """Fetch intraday + daily bars for all tickers.

        Returns ``{ticker: {'intraday': df, 'daily': df}}``.
        Tickers with no available data are omitted.
        """
        results: dict[str, dict[str, pd.DataFrame]] = {}
        for ticker in tickers:
            data: dict[str, pd.DataFrame] | None = await self._fetch_ticker_data(
                ticker,
                TICKER_EXCHANGE.get(ticker, Exchange.NYSE).value,
                target_date,
            )
            if data is not None:
                results[ticker] = data
            else:
                logger.debug("Skipping %s — no data available", ticker)
        logger.info(
            "Fetched data for %d / %d tickers", len(results), len(tickers)
        )
        return results

    async def _fetch_ticker_data(
        self,
        ticker: str,
        exchange: str,
        target_date: date,
    ) -> dict[str, pd.DataFrame] | None:
        """Fetch 1-min bars for target_date and 120 daily bars from IB.

        Returns ``None`` if data unavailable (e.g. OTC penny stocks, no
        trading on that day).

        When caching is enabled (the default), raw 1-min and daily bars
        are read from / written to the local parquet cache.
        """
        exch_enum: Exchange = TICKER_EXCHANGE.get(ticker, Exchange.NYSE)

        # --- Try cache first ---
        df_1min: pd.DataFrame | None = None
        df_daily: pd.DataFrame | None = None

        if self._use_cache:
            df_1min = load_cached(ticker, target_date, "intraday")
            df_daily = load_cached(ticker, target_date, "daily")

        if df_1min is None:
            logger.warning("No cached intraday data for %s on %s", ticker, target_date)
            return None
        if df_daily is None:
            df_daily = pd.DataFrame()

        # At this point df_1min and df_daily are resolved
        assert df_1min is not None  # noqa: S101
        if df_daily is None:
            df_daily = pd.DataFrame()

        df_5min: pd.DataFrame = self._resample_to_5min(df_1min)
        if df_5min.empty:
            logger.debug("Empty 5-min bars after resample for %s", ticker)
            return None

        return {"intraday": df_5min, "daily": df_daily}

    # ------------------------------------------------------------------
    # Resampling
    # ------------------------------------------------------------------

    def _resample_to_5min(self, df_1min: pd.DataFrame) -> pd.DataFrame:
        """Resample 1-min OHLCV to 5-min bars using left-closed, left-labeled bins."""
        if df_1min.empty:
            return df_1min

        ohlcv: pd.DataFrame = df_1min[["open", "high", "low", "close", "volume"]].copy()

        resampled: pd.DataFrame = ohlcv.resample("5min", closed="left", label="left").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        resampled = resampled.dropna(subset=["open", "close"])
        return resampled

    # ------------------------------------------------------------------
    # Simulation helpers
    # ------------------------------------------------------------------

    def _simulate_fill(self, price: float, side: str) -> float:
        """Apply slippage: buys pay more, sells receive less.

        Slippage = ``slippage_bps_per_side`` basis points of price.
        ``side`` must be ``'buy'`` or ``'sell'``.
        """
        slippage: float = price * (self._slippage_bps / 10_000.0)
        if side == "buy":
            return price + slippage
        return price - slippage

    @staticmethod
    def _calc_commission(
        ticker: str,
        exchange: str,
        qty: int,
        price: float,
        is_buy: bool,
    ) -> float:
        """Alpaca is commission-free. Always returns 0."""
        return 0.0

    def _position_size(
        self,
        ticker: str,
        exchange: str,
        signal_price: float,
        stop_price: float,
        equity: float,
        sentiment_score: float | None,
        atr_rank: float,
        phase: int,
    ) -> int:
        """Lightweight position sizer matching PositionSizer logic but
        without settlement/FX manager dependencies.

        Returns 0 if sizing fails any constraint.
        """
        phase_key: str = f"phase{phase}"
        risk_pct: float = float(self._config._require("risk", "risk_per_trade_pct", phase_key))
        max_pos_pct: float = float(self._config._require("risk", "max_position_pct", phase_key))

        # All prices are in USD (Alpaca, US-only)
        gbp_usd: float = self._config.fx_fallback_gbp_usd
        fx_to_gbp: float = 1.0 / gbp_usd

        stop_dist: float = abs(signal_price - stop_price)
        if stop_dist <= 0:
            return 0

        stop_dist_gbp: float = stop_dist * fx_to_gbp
        max_risk_gbp: float = equity * risk_pct
        shares_from_risk: int = math.floor(max_risk_gbp / stop_dist_gbp)

        # Cap by max_position_pct
        max_pos_gbp: float = equity * max_pos_pct
        max_pos_usd: float = max_pos_gbp / fx_to_gbp if fx_to_gbp > 0 else max_pos_gbp
        max_shares_pos: int = math.floor(max_pos_usd / signal_price) if signal_price > 0 else 0

        shares: int = min(shares_from_risk, max_shares_pos)

        # ATR high-vol reduction
        atr_high: float = float(self._config._require("strategy", "atr", "high_percentile"))
        atr_reduction: float = float(
            self._config._get("strategy", "atr", "high_vol_size_reduction", default=0.75)
        )
        if atr_rank > atr_high:
            shares = math.floor(shares * atr_reduction)

        # Sentiment reduction (no data = 75% size)
        no_data_mult: float = float(
            self._config._get("entry", "no_data_size_multiplier", default=0.75)
        )
        if sentiment_score is None:
            shares = math.floor(shares * no_data_mult)

        if shares <= 0:
            return 0

        # Min position value
        min_val: float = self._config.get_min_position_value(Market.US)
        pos_value_usd: float = shares * signal_price
        if pos_value_usd < min_val:
            logger.debug(
                "%s: position value USD%.2f below minimum USD%.2f",
                ticker, pos_value_usd, min_val,
            )
            return 0

        return shares

    # ------------------------------------------------------------------
    # Entry evaluation
    # ------------------------------------------------------------------

    def _record_rejection(self, ticker: str, reason: str) -> None:
        """Increment rejection counter for diagnostics."""
        if ticker not in self._rejection_counts:
            self._rejection_counts[ticker] = {}
        counts: dict[str, int] = self._rejection_counts[ticker]
        counts[reason] = counts.get(reason, 0) + 1

    def _record_signal_detail(self, ticker: str, signal_count: int) -> None:
        """Track signal count distribution for insufficient_signals rejections."""
        if ticker not in self._signal_detail_counts:
            self._signal_detail_counts[ticker] = {}
        detail: dict[int, int] = self._signal_detail_counts[ticker]
        detail[signal_count] = detail.get(signal_count, 0) + 1

    def _check_entry(
        self,
        ticker: str,
        exchange: str,
        df_5min: pd.DataFrame,
        df_daily: pd.DataFrame,
        bar_idx: int,
        current_time: datetime,
        open_positions: list[BacktestTrade],
        equity: float,
        daily_pnl: float,
        trade_count: int,
        cooldowns: dict[str, datetime],
        phase: int,
    ) -> BacktestTrade | None:
        """Evaluate entry at bar_idx. Returns a new BacktestTrade or None.

        Checks (in order):
          1. Daily trade limit
          2. Daily loss limit
          3. Max open positions
          4. ATR rank (skip if extreme)
          5. Signal count >= min_signals_required
          6. Direction must not be None (no conflicting signals)
          7. Ticker-level cooldown
          8. Sector exposure limit
          9. Position sizing (returns 0 → skip)
        """
        phase_key: str = f"phase{phase}"

        # 1. Daily trade limit
        max_daily: int = int(self._config._require("risk", "max_daily_trades", phase_key))
        if trade_count >= max_daily:
            self._record_rejection(ticker, "daily_trade_limit")
            return None

        # 2. Daily loss limit
        daily_loss_limit: float = self._config.daily_loss_limit_pct * equity
        if daily_pnl <= -daily_loss_limit:
            logger.debug("Daily loss limit reached (%.2f) — no new entries", daily_pnl)
            self._record_rejection(ticker, "daily_loss_limit")
            return None

        # 3. Max positions
        max_pos: int = int(self._config._require("risk", "max_positions", phase_key))
        if len(open_positions) >= max_pos:
            self._record_rejection(ticker, "max_positions")
            return None

        # 4. Ticker cooldown
        if ticker in cooldowns and current_time < cooldowns[ticker]:
            self._record_rejection(ticker, "cooldown")
            return None

        # 5. Need enough bars for indicator warm-up
        if bar_idx < 30:
            self._record_rejection(ticker, "warmup")
            return None

        # Slice up to and including current bar for signal evaluation
        df_slice: pd.DataFrame = df_5min.iloc[: bar_idx + 1]

        # Ensure indicators are computed on the slice
        if "ema_fast" not in df_slice.columns:
            df_slice = self._analyzer.compute_indicators(df_slice)

        signals: dict[str, Any] = self._analyzer.get_signals(df_slice, df_daily)
        atr_rank: float = signals["atr_rank"]

        # 6. ATR rank extreme — skip
        if atr_rank >= self._atr_extreme:
            logger.debug("%s: ATR rank %.1f >= extreme %.1f — skipping", ticker, atr_rank, self._atr_extreme)
            self._record_rejection(ticker, "atr_extreme")
            return None

        # 7. Signal count
        if signals["signal_count"] < self._min_signals:
            self._record_rejection(ticker, "insufficient_signals")
            self._record_signal_detail(ticker, signals["signal_count"])
            return None

        # 8. Direction must be unambiguous
        direction: str | None = signals["direction"]
        if direction is None:
            self._record_rejection(ticker, "no_direction")
            return None

        # Only long entries (bot is long-only in Phase 1)
        if direction != "long":
            self._record_rejection(ticker, "short_only")
            return None

        # 9. Sector exposure
        max_sector: int = int(self._config._require("risk", "max_sector_exposure", phase_key))
        sector: str = GICS_SECTOR.get(ticker, "Unknown")
        sector_count: int = sum(
            1 for p in open_positions if GICS_SECTOR.get(p.ticker, "") == sector
        )
        if sector_count >= max_sector:
            logger.debug("%s: sector '%s' already at max %d", ticker, sector, max_sector)
            self._record_rejection(ticker, "sector_limit")
            return None

        # 10. Sentiment: use neutral default (0.0) in backtest — no live data
        sentiment_score: float | None = None  # signals that we used neutral
        # If the default is non-zero, use it
        if self._default_sentiment != 0.0:
            sentiment_score = self._default_sentiment

        # Sentiment block check using default
        effective_sentiment: float = self._default_sentiment
        if effective_sentiment < self._sentiment_block:
            return None

        # 11. Signal price = close of current bar
        bar: pd.Series = df_5min.iloc[bar_idx]
        signal_price: float = float(bar["close"])
        if signal_price <= 0:
            return None

        # 12. Determine hold_type (intraday default)
        hold_type: HoldType = HoldType.INTRADAY

        # 13. Stop and target from exit params
        exit_params: dict[str, Any] = self._exit_intraday if hold_type == HoldType.INTRADAY else self._exit_swing
        stop_loss_pct: float = float(exit_params["stop_loss_pct"])
        take_profit_pct: float = float(exit_params["take_profit_pct"])
        stop_price: float = signal_price * (1.0 - stop_loss_pct)
        target_price: float = signal_price * (1.0 + take_profit_pct)

        # 14. Position sizing
        qty: int = self._position_size(
            ticker, exchange, signal_price, stop_price, equity,
            sentiment_score, atr_rank, phase,
        )
        if qty <= 0:
            return None

        # 15. Simulated fill with slippage
        fill_price: float = self._simulate_fill(signal_price, "buy")
        slippage_bps: float = abs(fill_price - signal_price) / signal_price * 10_000

        # Adjust stop/target to use fill price as basis for consistency
        stop_price_fill: float = fill_price * (1.0 - stop_loss_pct)
        target_price_fill: float = fill_price * (1.0 + take_profit_pct)

        # 16. Commission (buy side)
        buy_comm: float = self._calc_commission(ticker, exchange, qty, signal_price, True)

        trade: BacktestTrade = BacktestTrade(
            ticker=ticker,
            exchange=exchange,
            currency="USD",
            entry_time=current_time,
            entry_price=fill_price,
            signal_price=signal_price,
            quantity=qty,
            hold_type=hold_type.value,
            signals=signals,
            sentiment_score=sentiment_score,
            atr_rank=atr_rank,
            stop_price=stop_price_fill,
            target_price=target_price_fill,
            highest_price=fill_price,
            commission=buy_comm,
            slippage_bps=slippage_bps,
        )

        logger.info(
            "ENTRY %s %s qty=%d entry=%.4f stop=%.4f target=%.4f slippage=%.2fbps",
            ticker, hold_type.value, qty, fill_price, stop_price_fill, target_price_fill, slippage_bps,
        )
        return trade

    # ------------------------------------------------------------------
    # Exit evaluation
    # ------------------------------------------------------------------

    def _check_exits(
        self,
        positions: list[BacktestTrade],
        bar: pd.Series,
        current_time: datetime,
    ) -> list[tuple[BacktestTrade, str, float]]:
        """Check all open positions for exit conditions.

        Uses bar HIGH and LOW for stop/target detection — not just close —
        to detect intrabar breaches.

        Returns a list of ``(trade, exit_reason, exit_price)`` tuples.
        """
        exits: list[tuple[BacktestTrade, str, float]] = []
        bar_high: float = float(bar.get("high", bar["close"]))
        bar_low: float = float(bar.get("low", bar["close"]))
        bar_close: float = float(bar["close"])
        ticker: str = str(bar.name) if hasattr(bar, "name") else ""

        for trade in positions:
            if trade.ticker != ticker:
                continue

            reason: str | None = None
            exit_price: float = bar_close

            # Update highest price for trailing
            if trade.highest_price is None or bar_high > trade.highest_price:
                trade.highest_price = bar_high

            # --- Trailing stop activation ---
            if not trade.trailing_active:
                exit_params: dict[str, Any] = (
                    self._exit_intraday if trade.hold_type == HoldType.INTRADAY.value
                    else self._exit_swing
                )
                trailing_act_pct: float = float(exit_params["trailing_activation_pct"])
                if bar_high >= trade.entry_price * (1.0 + trailing_act_pct):
                    trade.trailing_active = True
                    logger.debug(
                        "%s: trailing stop activated at bar_high=%.4f",
                        trade.ticker, bar_high,
                    )

            # --- Stop loss (uses bar LOW) ---
            if bar_low <= trade.stop_price:
                reason = "stop_loss"
                # Simulate fill at stop price (could gap through)
                exit_price = min(trade.stop_price, bar_close)

            # --- Take profit (uses bar HIGH) ---
            elif bar_high >= trade.target_price:
                reason = "take_profit"
                exit_price = trade.target_price  # limit fill at target

            # --- Trailing stop (uses bar LOW vs trailing level) ---
            elif trade.trailing_active and trade.highest_price is not None:
                exit_params_t: dict[str, Any] = (
                    self._exit_intraday if trade.hold_type == HoldType.INTRADAY.value
                    else self._exit_swing
                )
                trail_dist_pct: float = float(exit_params_t["trailing_distance_pct"])
                trail_stop: float = trade.highest_price * (1.0 - trail_dist_pct)
                if bar_low <= trail_stop:
                    reason = "trailing_stop"
                    exit_price = min(trail_stop, bar_close)

            # --- Time stop for intraday (no end-of-day here — handled in walk_bars) ---
            elif trade.hold_type == HoldType.INTRADAY.value:
                exit_params_i: dict[str, Any] = self._exit_intraday
                time_stop_hours: float = float(exit_params_i.get("time_stop_hours", 4))
                flat_threshold: float = float(exit_params_i.get("time_stop_flat_threshold", 0.005))
                hold_hours: float = (current_time - trade.entry_time).total_seconds() / 3600
                if hold_hours >= time_stop_hours:
                    pnl_pct: float = (bar_close - trade.entry_price) / trade.entry_price
                    if abs(pnl_pct) <= flat_threshold:
                        reason = "time_stop"
                        exit_price = bar_close

            if reason is not None:
                exits.append((trade, reason, exit_price))

        return exits

    # ------------------------------------------------------------------
    # Core walk
    # ------------------------------------------------------------------

    def _walk_bars(
        self,
        all_data: dict[str, dict[str, pd.DataFrame]],
        target_date: date,
        initial_equity: float,
        phase: int,
    ) -> BacktestResult:
        """Walk through bars chronologically, executing the strategy.

        Algorithm:
          1. Pre-compute 5-min indicators for each ticker.
          2. Build a merged timeline of (timestamp, ticker, bar_idx) events.
          3. For each timestep:
             a. Process exits for all open positions on tickers with a bar.
             b. Check entries for all tickers in the bar (respecting limits).
          4. At end-of-day: force-close all intraday positions at last price.
          5. Accumulate equity curve and track max drawdown.
        """
        backtest_id: str = str(uuid.uuid4())
        run_date: str = datetime.now(TZ_EASTERN).date().isoformat()

        skipped_checks: list[str] = [
            "Bid-ask spread: not available in historical bars",
            "Earnings blackout: no cached data for backtest date",
            "Real-time sentiment: used neutral (0.0) for all tickers",
        ]

        # Pre-compute indicators for all tickers
        enriched: dict[str, pd.DataFrame] = {}
        for ticker, data in all_data.items():
            try:
                df_ind: pd.DataFrame = self._analyzer.compute_indicators(data["intraday"])
                enriched[ticker] = df_ind
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to compute indicators for %s: %s", ticker, exc)

        if not enriched:
            logger.warning("No enriched data for any ticker")
            return BacktestResult(
                backtest_id=backtest_id,
                run_date=run_date,
                target_date=target_date.isoformat(),
                initial_equity=initial_equity,
                final_equity=initial_equity,
                trades=[],
                skipped_checks=skipped_checks,
            )

        # Build sorted timeline: list of (timestamp_utc, ticker, bar_idx)
        # Each entry is one 5-min bar for one ticker
        events: list[tuple[pd.Timestamp, str, int]] = []
        for ticker, df_ind in enriched.items():
            for idx in range(len(df_ind)):
                ts: pd.Timestamp = df_ind.index[idx]
                events.append((ts, ticker, idx))

        events.sort(key=lambda e: e[0])

        # State
        equity: float = initial_equity
        peak_equity: float = initial_equity
        max_drawdown_pct: float = 0.0
        daily_pnl: float = 0.0
        trade_count: int = 0
        open_positions: list[BacktestTrade] = []
        closed_trades: list[BacktestTrade] = []
        cooldowns: dict[str, datetime] = {}

        # Execution windows
        us_exec_start = self._config.get_execution_start(Market.US)     # ET time
        us_exec_end = self._config.get_execution_end(Market.US)

        # Track last bar per ticker for end-of-day close
        last_bar_per_ticker: dict[str, tuple[pd.Series, datetime]] = {}

        def _in_execution_window(ts: pd.Timestamp, exch: Exchange) -> bool:
            """Check if timestamp falls within the execution window."""
            t_eastern: datetime = ts.tz_convert(TZ_EASTERN).to_pydatetime()
            t = t_eastern.time()
            return us_exec_start <= t <= us_exec_end

        def _bar_as_series(ticker: str, bar_idx: int) -> pd.Series:
            df_t: pd.DataFrame = enriched[ticker]
            row: pd.Series = df_t.iloc[bar_idx].copy()
            row.name = ticker  # embed ticker in the series name for _check_exits
            return row

        def _current_time_for_ts(ts: pd.Timestamp, exch: Exchange) -> datetime:
            """Convert bar timestamp to aware datetime in US/Eastern."""
            return ts.tz_convert(TZ_EASTERN).to_pydatetime()

        def _close_trade(
            trade: BacktestTrade,
            exit_price: float,
            exit_reason: str,
            exit_time: datetime,
        ) -> None:
            nonlocal equity, daily_pnl, peak_equity, max_drawdown_pct

            fill_exit: float = self._simulate_fill(exit_price, "sell")
            gross_pnl: float = (fill_exit - trade.entry_price) * trade.quantity

            # Convert USD P&L to GBP for equity tracking
            gbp_usd: float = self._config.fx_fallback_gbp_usd
            net_pnl_gbp: float = gross_pnl / gbp_usd

            trade.exit_time = exit_time
            trade.exit_price = fill_exit
            trade.exit_reason = exit_reason
            trade.commission = 0.0
            trade.gross_pnl = gross_pnl
            trade.net_pnl = net_pnl_gbp

            equity += net_pnl_gbp
            daily_pnl += net_pnl_gbp

            if equity > peak_equity:
                peak_equity = equity
            drawdown: float = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
            if drawdown > max_drawdown_pct:
                max_drawdown_pct = drawdown

            # Set cooldown for this ticker
            cooldown_until: datetime = exit_time + timedelta(minutes=self._cooldown_minutes)
            cooldowns[trade.ticker] = cooldown_until

            logger.info(
                "EXIT %s qty=%d exit=%.4f reason=%s net_pnl=%.2f GBP equity=%.2f",
                trade.ticker, trade.quantity, fill_exit, exit_reason, net_pnl_gbp, equity,
            )
            open_positions.remove(trade)
            closed_trades.append(trade)

        # --- Main loop ---
        for ts, ticker, bar_idx in events:
            if ticker not in enriched:
                continue

            exch_enum: Exchange = TICKER_EXCHANGE.get(ticker, Exchange.NYSE)
            exchange: str = exch_enum.value
            current_time: datetime = _current_time_for_ts(ts, exch_enum)

            bar: pd.Series = _bar_as_series(ticker, bar_idx)
            last_bar_per_ticker[ticker] = (bar, current_time)

            # --- Process exits first ---
            positions_for_ticker: list[BacktestTrade] = [
                p for p in list(open_positions) if p.ticker == ticker
            ]
            if positions_for_ticker:
                exits: list[tuple[BacktestTrade, str, float]] = self._check_exits(
                    positions_for_ticker, bar, current_time
                )
                for (trade, reason, exit_px) in exits:
                    _close_trade(trade, exit_px, reason, current_time)

            # --- Check entries (only during execution window) ---
            if _in_execution_window(ts, exch_enum):
                # Only enter if we have no open position for this ticker already
                already_in: bool = any(p.ticker == ticker for p in open_positions)
                if not already_in:
                    new_trade: BacktestTrade | None = self._check_entry(
                        ticker=ticker,
                        exchange=exchange,
                        df_5min=enriched[ticker],
                        df_daily=all_data[ticker].get("daily", pd.DataFrame()),
                        bar_idx=bar_idx,
                        current_time=current_time,
                        open_positions=open_positions,
                        equity=equity,
                        daily_pnl=daily_pnl,
                        trade_count=trade_count,
                        cooldowns=cooldowns,
                        phase=phase,
                    )
                    if new_trade is not None:
                        open_positions.append(new_trade)
                        trade_count += 1  # count entries, not exits

        # --- End of day: close all intraday positions at last available price ---
        for trade in list(open_positions):
            if trade.hold_type != HoldType.INTRADAY.value:
                # Swing trades carry over; close them too for single-day backtest
                pass

            ticker_t: str = trade.ticker
            if ticker_t in last_bar_per_ticker:
                last_bar, last_time = last_bar_per_ticker[ticker_t]
                close_px: float = float(last_bar["close"])
            else:
                close_px = trade.entry_price  # fallback: flat
                last_time = datetime.now(TZ_EASTERN)

            _close_trade(trade, close_px, "wind_down", last_time)

        all_trades: list[BacktestTrade] = closed_trades

        # --- Log per-ticker signal rejection summary ---
        if self._rejection_counts:
            lines: list[str] = ["Signal rejection summary per ticker:"]
            for t in sorted(self._rejection_counts):
                parts: list[str] = [
                    f"{reason}={count}"
                    for reason, count in sorted(self._rejection_counts[t].items())
                ]
                line: str = f"  {t:8s} {' '.join(parts)}"
                # Append signal detail breakdown if available
                if t in self._signal_detail_counts:
                    detail_parts: list[str] = [
                        f"{sc}\u00d7{n}"
                        for sc, n in sorted(self._signal_detail_counts[t].items())
                    ]
                    line += f"  | signals: {', '.join(detail_parts)}  (needed {self._min_signals})"
                lines.append(line)
            logger.info("\n".join(lines))

        # Reset counters for next run
        self._rejection_counts = {}
        self._signal_detail_counts = {}

        result: BacktestResult = BacktestResult(
            backtest_id=backtest_id,
            run_date=run_date,
            target_date=target_date.isoformat(),
            initial_equity=initial_equity,
            final_equity=equity,
            trades=all_trades,
            max_drawdown_pct=max_drawdown_pct * 100.0,  # store as percent
            skipped_checks=skipped_checks,
        )
        return result

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    def _calc_metrics(self, result: BacktestResult) -> BacktestResult:
        """Populate aggregate statistics on *result* in-place and return it."""
        trades: list[BacktestTrade] = result.trades
        if not trades:
            result.total_trades = 0
            result.wins = 0
            result.losses = 0
            result.win_rate = 0.0
            result.profit_factor = None
            result.avg_hold_minutes = 0.0
            return result

        total: int = len(trades)
        wins: int = sum(1 for t in trades if t.net_pnl > 0)
        losses: int = sum(1 for t in trades if t.net_pnl <= 0)
        gross_pnl: float = sum(t.gross_pnl for t in trades)
        commissions: float = sum(t.commission for t in trades)
        net_pnl: float = sum(t.net_pnl for t in trades)

        total_wins_pnl: float = sum(t.net_pnl for t in trades if t.net_pnl > 0)
        total_losses_pnl: float = abs(sum(t.net_pnl for t in trades if t.net_pnl < 0))
        profit_factor: float | None = (
            (total_wins_pnl / total_losses_pnl) if total_losses_pnl > 0 else None
        )

        hold_minutes: list[float] = []
        for t in trades:
            if t.exit_time is not None:
                delta: float = (t.exit_time - t.entry_time).total_seconds() / 60.0
                hold_minutes.append(delta)
        avg_hold: float = sum(hold_minutes) / len(hold_minutes) if hold_minutes else 0.0

        result.total_trades = total
        result.wins = wins
        result.losses = losses
        result.gross_pnl = gross_pnl
        result.commissions = commissions
        result.net_pnl = net_pnl
        result.win_rate = wins / total if total > 0 else 0.0
        result.profit_factor = profit_factor
        result.avg_hold_minutes = avg_hold

        return result

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save_result(self, result: BacktestResult, db_path: str) -> None:
        """Save backtest result to the ``backtest_results`` table."""
        # Build a serialisable version of trades
        trades_json: str = json.dumps(
            [
                {
                    "ticker": t.ticker,
                    "exchange": t.exchange,
                    "currency": t.currency,
                    "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                    "entry_price": t.entry_price,
                    "signal_price": t.signal_price,
                    "quantity": t.quantity,
                    "hold_type": t.hold_type,
                    "sentiment_score": t.sentiment_score,
                    "atr_rank": t.atr_rank,
                    "stop_price": t.stop_price,
                    "target_price": t.target_price,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "highest_price": t.highest_price,
                    "trailing_active": t.trailing_active,
                    "commission": t.commission,
                    "gross_pnl": t.gross_pnl,
                    "net_pnl": t.net_pnl,
                    "slippage_bps": t.slippage_bps,
                }
                for t in result.trades
            ]
        )

        params_json: str = json.dumps({
            "slippage_bps_per_side": self._slippage_bps,
            "commission_model": "ib_tiered",
            "phase": "auto",
        })

        record: dict[str, Any] = {
            "backtest_id": result.backtest_id,
            "run_date": result.run_date,
            "start_date": result.target_date,
            "end_date": result.target_date,
            "initial_equity": result.initial_equity,
            "final_equity": result.final_equity,
            "total_trades": result.total_trades,
            "wins": result.wins,
            "losses": result.losses,
            "gross_pnl": result.gross_pnl,
            "commissions": result.commissions,
            "net_pnl": result.net_pnl,
            "max_drawdown_pct": result.max_drawdown_pct,
            "sharpe_ratio": None,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "avg_hold_minutes": result.avg_hold_minutes,
            "slippage_model": f"{self._slippage_bps:.1f}bps_per_side",
            "parameters_json": params_json,
            "trades_json": trades_json,
            "notes": f"skipped_checks={len(result.skipped_checks)}",
        }

        conn: sqlite3.Connection = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            save_backtest_result(conn, record)
            logger.info(
                "Saved backtest result %s to %s", result.backtest_id, db_path
            )
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_backtest_report(result: BacktestResult) -> str:
    """Return a multi-line formatted backtest report string."""
    lines: list[str] = []
    W: int = 42  # line width

    def _sep(char: str = "=") -> str:
        return char * W

    net_sign: str = "+" if result.net_pnl >= 0 else ""
    net_pct: float = (
        (result.net_pnl / result.initial_equity * 100) if result.initial_equity else 0.0
    )
    net_pct_sign: str = "+" if net_pct >= 0 else ""

    lines.append(_sep())
    lines.append(f"BACKTEST REPORT: {result.target_date}")
    lines.append(_sep())
    lines.append(f"Starting Equity:  £{result.initial_equity:,.2f}")
    lines.append(f"Ending Equity:    £{result.final_equity:,.2f}")
    lines.append(
        f"Net P&L:          {net_sign}£{result.net_pnl:.2f} "
        f"({net_pct_sign}{net_pct:.2f}%)"
    )
    lines.append("")
    lines.append(
        f"Trades:           {result.total_trades}  |  "
        f"Wins: {result.wins}  |  Losses: {result.losses}"
    )
    win_pct: float = result.win_rate * 100
    lines.append(f"Win Rate:         {win_pct:.1f}%")

    if result.profit_factor is not None:
        lines.append(f"Profit Factor:    {result.profit_factor:.1f}x")
    else:
        lines.append("Profit Factor:    N/A")

    lines.append(f"Max Drawdown:     -{result.max_drawdown_pct:.1f}%")
    lines.append(f"Avg Hold Time:    {result.avg_hold_minutes:.0f} min")
    lines.append("")
    lines.append(_sep("-")[: W - 2] + " PER TRADE " + _sep("-")[: W - 2])

    # Column header
    lines.append(
        f"{'TIME':<10} {'TICKER':<7} {'QTY':>5}  {'ENTRY':>8}  {'EXIT':>8}  "
        f"{'P&L':>9}  REASON"
    )

    for trade in result.trades:
        # Format entry time in the exchange's local time
        t_local: datetime = trade.entry_time.astimezone(TZ_EASTERN)
        t_str: str = f"{t_local.strftime('%H:%M')} ET"

        exit_px_str: str = f"{trade.exit_price:.4f}" if trade.exit_price is not None else "OPEN"
        pnl_sign: str = "+" if trade.net_pnl >= 0 else ""
        pnl_str: str = f"{pnl_sign}£{trade.net_pnl:.2f}"
        reason_str: str = trade.exit_reason or "open"

        lines.append(
            f"{t_str:<10} {trade.ticker:<7} {trade.quantity:>5}  "
            f"{trade.entry_price:>8.4f}  {exit_px_str:>8}  "
            f"{pnl_str:>9}  {reason_str}"
        )

    if not result.trades:
        lines.append("  (no trades)")

    lines.append("")
    lines.append(_sep("-")[: W - 2] + " SKIPPED CHECKS " + _sep("-")[: W - 2])
    for check in result.skipped_checks:
        lines.append(f"  * {check}")

    lines.append("")
    lines.append(
        "NOTE: Backtest results are indicative only. Spread and sentiment"
    )
    lines.append(
        "data were not available for this date."
    )
    lines.append(_sep())

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Multi-day summary
# ---------------------------------------------------------------------------


def _get_trading_days(
    start: date, end: date, config: Config
) -> list[date]:
    """Return trading days (weekdays excluding holidays) between *start* and *end* inclusive."""
    holidays_raw: dict[str, Any] = config._raw.get("holidays", {})
    holiday_dates: set[str] = set()
    for year_holidays in holidays_raw.values():
        if isinstance(year_holidays, dict):
            for market_holidays in year_holidays.values():
                if isinstance(market_holidays, list):
                    for h in market_holidays:
                        holiday_dates.add(str(h))

    days: list[date] = []
    d: date = start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holiday_dates:
            days.append(d)
        d += timedelta(days=1)
    return days


def format_multi_day_summary(
    results: list[BacktestResult],
    initial_equity: float,
) -> str:
    """Format a combined summary for multiple backtest days."""
    lines: list[str] = []
    W: int = 90

    lines.append("=" * W)
    lines.append("MULTI-DAY BACKTEST SUMMARY")
    lines.append("=" * W)

    if not results:
        lines.append("  (no results)")
        lines.append("=" * W)
        return "\n".join(lines)

    # Per-day summary table
    lines.append(
        f"{'DATE':<12} {'TRADES':>6} {'WINS':>5} {'LOSSES':>6} "
        f"{'NET P&L':>10} {'EQUITY':>10} {'RETURN':>8} {'MAX DD':>8}"
    )
    lines.append("-" * W)

    equity: float = initial_equity
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_net_pnl: float = 0.0
    total_commission: float = 0.0
    peak_equity: float = equity
    max_drawdown_pct: float = 0.0
    trading_days_with_trades: int = 0
    daily_returns: list[float] = []

    for r in results:
        equity += r.net_pnl
        total_trades += r.total_trades
        total_wins += r.wins
        total_losses += r.losses
        total_net_pnl += r.net_pnl
        total_commission += r.commissions
        if r.total_trades > 0:
            trading_days_with_trades += 1

        day_return_pct: float = (r.net_pnl / (equity - r.net_pnl) * 100) if (equity - r.net_pnl) > 0 else 0.0
        daily_returns.append(day_return_pct)

        peak_equity = max(peak_equity, equity)
        dd: float = ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0
        max_drawdown_pct = max(max_drawdown_pct, dd)

        pnl_sign: str = "+" if r.net_pnl >= 0 else ""
        ret_sign: str = "+" if day_return_pct >= 0 else ""
        lines.append(
            f"{r.target_date:<12} {r.total_trades:>6} {r.wins:>5} {r.losses:>6} "
            f"{pnl_sign}£{r.net_pnl:>8.2f} £{equity:>9.2f} "
            f"{ret_sign}{day_return_pct:>6.2f}% {-r.max_drawdown_pct:>7.1f}%"
        )

    lines.append("-" * W)

    # Aggregate stats
    total_return_pct: float = (total_net_pnl / initial_equity * 100) if initial_equity > 0 else 0.0
    win_rate: float = (total_wins / total_trades * 100) if total_trades > 0 else 0.0

    # Profit factor
    gross_wins: float = sum(
        t.net_pnl for r in results for t in r.trades if t.net_pnl > 0
    )
    gross_losses: float = abs(sum(
        t.net_pnl for r in results for t in r.trades if t.net_pnl < 0
    ))

    # Sharpe-like ratio (daily returns)
    import statistics
    avg_daily: float = statistics.mean(daily_returns) if daily_returns else 0.0
    std_daily: float = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0

    lines.append("")
    lines.append(f"Period:           {results[0].target_date} to {results[-1].target_date} ({len(results)} trading days)")
    lines.append(f"Starting Equity:  £{initial_equity:,.2f}")
    lines.append(f"Ending Equity:    £{equity:,.2f}")

    pnl_sign = "+" if total_net_pnl >= 0 else ""
    ret_sign = "+" if total_return_pct >= 0 else ""
    lines.append(f"Net P&L:          {pnl_sign}£{total_net_pnl:,.2f} ({ret_sign}{total_return_pct:.2f}%)")
    lines.append(f"Commissions:      £{total_commission:,.2f}")
    lines.append("")
    lines.append(f"Total Trades:     {total_trades}  |  Wins: {total_wins}  |  Losses: {total_losses}")
    lines.append(f"Win Rate:         {win_rate:.1f}%")
    if gross_losses > 0:
        lines.append(f"Profit Factor:    {gross_wins / gross_losses:.2f}")
    else:
        lines.append(f"Profit Factor:    {'N/A' if total_trades == 0 else 'inf'}")
    lines.append(f"Max Drawdown:     -{max_drawdown_pct:.1f}%")
    lines.append(f"Active Days:      {trading_days_with_trades} / {len(results)} ({trading_days_with_trades / len(results) * 100:.0f}%)")
    lines.append(f"Avg Daily Return: {avg_daily:+.3f}%")
    if std_daily > 0:
        sharpe_approx: float = (avg_daily / std_daily) * (252 ** 0.5)
        lines.append(f"Sharpe (approx):  {sharpe_approx:.2f}  (annualised from daily returns)")
    lines.append("")

    # Per-trade list (abbreviated if many)
    all_trades: list[tuple[str, BacktestTrade]] = [
        (r.target_date, t) for r in results for t in r.trades
    ]
    if all_trades:
        lines.append("-" * W)
        lines.append(
            f"{'DATE':<12} {'TIME':<10} {'TICKER':<7} {'QTY':>5}  {'ENTRY':>8}  "
            f"{'EXIT':>8}  {'P&L':>9}  REASON"
        )
        max_show: int = 50
        for i, (dt, trade) in enumerate(all_trades):
            if i >= max_show:
                lines.append(f"  ... and {len(all_trades) - max_show} more trades")
                break
            tz: ZoneInfo = TZ_EASTERN
            tz_label: str = "ET"
            t_local: datetime = trade.entry_time.astimezone(tz)
            t_str: str = f"{t_local.strftime('%H:%M')} {tz_label}"
            exit_px: str = f"{trade.exit_price:.4f}" if trade.exit_price is not None else "OPEN"
            pnl_s: str = ("+" if trade.net_pnl >= 0 else "") + f"£{trade.net_pnl:.2f}"
            reason: str = trade.exit_reason or "open"
            lines.append(
                f"{dt:<12} {t_str:<10} {trade.ticker:<7} {trade.quantity:>5}  "
                f"{trade.entry_price:>8.4f}  {exit_px:>8}  {pnl_s:>9}  {reason}"
            )

    lines.append("")
    lines.append("=" * W)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Backtest results persistence & comparison
# ---------------------------------------------------------------------------


def _extract_key_params(config: Config) -> dict[str, Any]:
    """Extract the key tunable parameters from a Config for result storage."""
    return {
        "min_signals_required": config._get("entry", "min_signals_required"),
        "atr_extreme_percentile": config._get("strategy", "atr", "extreme_percentile"),
        "atr_high_percentile": config._get("strategy", "atr", "high_percentile"),
        "stop_loss_pct": config._get("exit_intraday", "stop_loss_pct"),
        "take_profit_pct": config._get("exit_intraday", "take_profit_pct"),
        "trailing_activation_pct": config._get("exit_intraday", "trailing_activation_pct"),
        "trailing_distance_pct": config._get("exit_intraday", "trailing_distance_pct"),
        "time_stop_flat_threshold": config._get("exit_intraday", "time_stop_flat_threshold"),
    }


def save_results_json(
    results: list[BacktestResult],
    initial_equity: float,
    config: Config,
    config_path: str,
    from_date: date,
    to_date: date,
) -> str:
    """Save multi-day backtest results to a structured JSON file.

    Returns the path of the written file.
    """
    import os
    import statistics

    # Derive config name from path (e.g. "config_backtest" from "config_backtest.yaml")
    config_name: str = os.path.splitext(os.path.basename(config_path))[0]

    # Compute aggregate metrics (mirrors format_multi_day_summary logic)
    equity: float = initial_equity
    total_trades: int = 0
    total_wins: int = 0
    total_losses: int = 0
    total_net_pnl: float = 0.0
    total_commission: float = 0.0
    peak_equity: float = equity
    max_drawdown_pct: float = 0.0
    trading_days_with_trades: int = 0
    daily_returns: list[float] = []
    daily_results_list: list[dict[str, Any]] = []

    for r in results:
        equity += r.net_pnl
        total_trades += r.total_trades
        total_wins += r.wins
        total_losses += r.losses
        total_net_pnl += r.net_pnl
        total_commission += r.commissions
        if r.total_trades > 0:
            trading_days_with_trades += 1

        day_return_pct: float = (
            (r.net_pnl / (equity - r.net_pnl) * 100)
            if (equity - r.net_pnl) > 0
            else 0.0
        )
        daily_returns.append(day_return_pct)

        peak_equity = max(peak_equity, equity)
        dd: float = (
            ((peak_equity - equity) / peak_equity * 100) if peak_equity > 0 else 0.0
        )
        max_drawdown_pct = max(max_drawdown_pct, dd)

        daily_results_list.append({
            "date": r.target_date,
            "trades": r.total_trades,
            "net_pnl": round(r.net_pnl, 2),
            "equity": round(equity, 2),
        })

    total_return_pct: float = (
        (total_net_pnl / initial_equity * 100) if initial_equity > 0 else 0.0
    )
    win_rate: float = (total_wins / total_trades * 100) if total_trades > 0 else 0.0
    avg_daily: float = statistics.mean(daily_returns) if daily_returns else 0.0
    std_daily: float = (
        statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
    )
    sharpe_approx: float = (
        (avg_daily / std_daily) * (252 ** 0.5) if std_daily > 0 else 0.0
    )

    # Profit factor
    gross_wins: float = sum(
        t.net_pnl for r in results for t in r.trades if t.net_pnl > 0
    )
    gross_losses_abs: float = abs(sum(
        t.net_pnl for r in results for t in r.trades if t.net_pnl < 0
    ))
    profit_factor: float | None = (
        round(gross_wins / gross_losses_abs, 2) if gross_losses_abs > 0 else None
    )

    # Build trades list
    trades_list: list[dict[str, Any]] = []
    for r in results:
        for t in r.trades:
            trades_list.append({
                "date": r.target_date,
                "ticker": t.ticker,
                "qty": t.quantity,
                "entry": round(t.entry_price, 4),
                "exit": round(t.exit_price, 4) if t.exit_price is not None else None,
                "net_pnl": round(t.net_pnl, 2),
                "reason": t.exit_reason or "open",
            })

    now: datetime = datetime.now(tz=ZoneInfo("Europe/London"))
    payload: dict[str, Any] = {
        "config_name": config_name,
        "config_path": config_path,
        "from_date": from_date.isoformat(),
        "to_date": to_date.isoformat(),
        "initial_equity": initial_equity,
        "final_equity": round(equity, 2),
        "total_trades": total_trades,
        "wins": total_wins,
        "losses": total_losses,
        "net_pnl": round(total_net_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "commissions": round(total_commission, 2),
        "win_rate": round(win_rate, 1),
        "profit_factor": profit_factor,
        "max_drawdown_pct": round(max_drawdown_pct, 1),
        "sharpe_approx": round(sharpe_approx, 2),
        "active_days": trading_days_with_trades,
        "total_days": len(results),
        "avg_daily_return": round(avg_daily, 3),
        "run_timestamp": now.isoformat(),
        "key_params": _extract_key_params(config),
        "daily_results": daily_results_list,
        "trades": trades_list,
    }

    # Write to disk
    out_dir: str = os.path.join(os.getcwd(), "backtest_results")
    os.makedirs(out_dir, exist_ok=True)

    ts_str: str = now.strftime("%Y%m%dT%H%M%S")
    filename: str = (
        f"{config_name}_{from_date.isoformat()}_to_{to_date.isoformat()}_{ts_str}.json"
    )
    filepath: str = os.path.join(out_dir, filename)

    with open(filepath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    logger.info("Backtest results saved to %s", filepath)
    return filepath


def compare_results(paths: list[str]) -> str:
    """Load multiple backtest result JSON files and produce a comparison table."""
    import os

    data: list[dict[str, Any]] = []
    for p in paths:
        resolved: str = os.path.expanduser(p)
        try:
            with open(resolved, "r", encoding="utf-8") as fh:
                data.append(json.load(fh))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("Failed to load %s: %s", p, exc)
            continue

    if len(data) < 2:
        return "Need at least 2 result files to compare."

    # Column layout
    label_w: int = 24
    col_w: int = max(24, max(len(d.get("config_path", "?")) for d in data) + 2)
    total_w: int = label_w + col_w * len(data)

    lines: list[str] = []
    lines.append("=" * total_w)
    lines.append("BACKTEST COMPARISON")
    lines.append("=" * total_w)

    # Header row
    header: str = " " * label_w
    for d in data:
        header += f"{d.get('config_path', '?'):<{col_w}}"
    lines.append(header)
    lines.append("-" * total_w)

    def _row(label: str, values: list[str]) -> str:
        row: str = f"{label:<{label_w}}"
        for v in values:
            row += f"{v:<{col_w}}"
        return row

    # Period
    lines.append(_row(
        "Period",
        [f"{d['from_date']} to {d['to_date'][-5:]}" for d in data],
    ))
    # Starting equity
    lines.append(_row(
        "Starting Equity",
        [f"\u00a3{d['initial_equity']:,.2f}" for d in data],
    ))
    # Ending equity
    lines.append(_row(
        "Ending Equity",
        [f"\u00a3{d['final_equity']:,.2f}" for d in data],
    ))
    # Net P&L
    lines.append(_row(
        "Net P&L",
        [
            f"{'+' if d['net_pnl'] >= 0 else ''}\u00a3{d['net_pnl']:,.2f}"
            for d in data
        ],
    ))
    # Total return
    lines.append(_row(
        "Total Return",
        [
            f"{'+' if d['total_return_pct'] >= 0 else ''}{d['total_return_pct']:.2f}%"
            for d in data
        ],
    ))
    # Commissions
    lines.append(_row(
        "Commissions",
        [f"\u00a3{d['commissions']:,.2f}" for d in data],
    ))
    # Total trades
    lines.append(_row(
        "Total Trades",
        [str(d["total_trades"]) for d in data],
    ))
    # Win rate
    lines.append(_row(
        "Win Rate",
        [f"{d['win_rate']:.1f}%" for d in data],
    ))
    # Profit factor
    lines.append(_row(
        "Profit Factor",
        [
            f"{d['profit_factor']:.2f}" if d.get("profit_factor") is not None else "N/A"
            for d in data
        ],
    ))
    # Max drawdown
    lines.append(_row(
        "Max Drawdown",
        [f"-{d['max_drawdown_pct']:.1f}%" for d in data],
    ))
    # Avg daily return
    lines.append(_row(
        "Avg Daily Return",
        [f"{d['avg_daily_return']:+.3f}%" for d in data],
    ))
    # Sharpe
    lines.append(_row(
        "Sharpe (approx)",
        [f"{d['sharpe_approx']:.2f}" for d in data],
    ))
    # Active days
    lines.append(_row(
        "Active Days",
        [f"{d['active_days']}/{d['total_days']}" for d in data],
    ))

    # Key parameter diffs
    all_param_keys: list[str] = []
    for d in data:
        for k in d.get("key_params", {}):
            if k not in all_param_keys:
                all_param_keys.append(k)

    diff_rows: list[str] = []
    for k in all_param_keys:
        values: list[Any] = [d.get("key_params", {}).get(k) for d in data]
        # Only show params that differ between runs
        if len(set(str(v) for v in values)) > 1:
            formatted: list[str] = []
            for v in values:
                if isinstance(v, float) and v < 1.0:
                    formatted.append(f"{v * 100:.1f}%")
                else:
                    formatted.append(str(v))
            diff_rows.append(_row(f"  {k}", formatted))

    if diff_rows:
        lines.append("")
        lines.append("Key Parameter Diffs:")
        lines.extend(diff_rows)

    # Highlight best performer
    best_idx: int = max(range(len(data)), key=lambda i: data[i]["total_return_pct"])
    lines.append("")
    lines.append(
        f">>> Best: {data[best_idx].get('config_path', '?')} "
        f"({'+' if data[best_idx]['total_return_pct'] >= 0 else ''}"
        f"{data[best_idx]['total_return_pct']:.2f}% return)"
    )

    lines.append("=" * total_w)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """CLI entry point for ``python -m trading_bot.backtest``.

    Supports single-day (``--date``) or multi-day (``--from`` / ``--to``) mode.
    In multi-day mode, equity rolls forward between days (compounding).
    """
    from trading_bot.log_setup import setup_logging

    log_path = setup_logging("backtest")

    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=(
            "Run backtests against Alpaca historical data. "
            "Use --date for a single day, or --from/--to for a date range."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--date",
        help="Single target date (YYYY-MM-DD)",
    )
    group.add_argument(
        "--from",
        dest="from_date",
        help="Start date for multi-day backtest (YYYY-MM-DD). Requires --to.",
    )
    group.add_argument(
        "--compare",
        nargs="+",
        metavar="JSON_FILE",
        help="Compare previously saved backtest result JSON files.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        help="End date for multi-day backtest (YYYY-MM-DD). Requires --from.",
    )
    parser.add_argument(
        "--equity",
        type=float,
        default=950.0,
        help="Starting equity in GBP (default: 950.0)",
    )
    parser.add_argument(
        "--phase",
        type=int,
        default=1,
        choices=[1, 2, 3],
        help="Trading phase 1-3 (default: 1)",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="Do not persist results to SQLite",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Bypass cache reads (still writes fetched data to cache)",
    )
    args: argparse.Namespace = parser.parse_args()

    # --compare mode: load JSON files and print comparison, then exit
    if args.compare:
        import sys

        comparison: str = compare_results(args.compare)
        for line in comparison.splitlines():
            logger.info(line)
        sys.stdout.write(comparison + "\n")
        return

    # Validate date args
    if args.from_date and not args.to_date:
        parser.error("--from requires --to")
    if args.to_date and not args.from_date:
        parser.error("--to requires --from")

    # Build list of dates to backtest
    target_dates: list[date] = []
    config: Config = Config.load(args.config)

    if args.date:
        try:
            target_dates = [date.fromisoformat(args.date)]
        except ValueError as exc:
            logger.error("Invalid date '%s': %s", args.date, exc)
            raise SystemExit(1) from exc
    else:
        try:
            d_from: date = date.fromisoformat(args.from_date)
            d_to: date = date.fromisoformat(args.to_date)
        except ValueError as exc:
            logger.error("Invalid date: %s", exc)
            raise SystemExit(1) from exc
        if d_from > d_to:
            parser.error("--from date must be before --to date")
        target_dates = _get_trading_days(d_from, d_to, config)
        logger.info(
            "Multi-day backtest: %s to %s (%d trading days)",
            d_from.isoformat(),
            d_to.isoformat(),
            len(target_dates),
        )

    if not target_dates:
        logger.error("No trading days in the specified range.")
        raise SystemExit(1)

    engine: BacktestEngine = BacktestEngine(config, use_cache=not args.no_cache)
    results: list[BacktestResult] = []
    equity: float = args.equity
    multi_day: bool = len(target_dates) > 1

    try:
        for i, target_date in enumerate(target_dates):
            if multi_day:
                logger.info(
                    "--- Day %d/%d: %s (equity=£%.2f) ---",
                    i + 1, len(target_dates), target_date.isoformat(), equity,
                )

            result: BacktestResult = await engine.run(
                target_date=target_date,
                initial_equity=equity,
                phase=args.phase,
            )
            results.append(result)

            # Roll equity forward (compounding)
            equity = result.final_equity

            # Single-day: print full report immediately
            if not multi_day:
                report: str = format_backtest_report(result)
                for line in report.splitlines():
                    logger.info(line)
                import sys
                sys.stdout.write(report + "\n")

            # Save to DB
            if not args.no_save:
                try:
                    engine.save_result(result, config.db_path)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to save result for %s: %s", target_date, exc)

    finally:
        pass

    # Multi-day: print combined summary and save results JSON
    if multi_day:
        import sys
        summary: str = format_multi_day_summary(results, args.equity)
        for line in summary.splitlines():
            logger.info(line)
        sys.stdout.write(summary + "\n")

        # Save structured results to JSON for later comparison
        try:
            d_from_save: date = date.fromisoformat(results[0].target_date)
            d_to_save: date = date.fromisoformat(results[-1].target_date)
            json_path: str = save_results_json(
                results=results,
                initial_equity=args.equity,
                config=config,
                config_path=args.config,
                from_date=d_from_save,
                to_date=d_to_save,
            )
            sys.stdout.write(f"\nResults saved to: {json_path}\n")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to save results JSON: %s", exc)


if __name__ == "__main__":
    asyncio.run(main())

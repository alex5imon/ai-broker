"""Multi-strategy backtester — runs all strategies in parallel over historical data.

Usage::

    # Alpaca intraday bars (requires downloaded data)
    python -m trading_bot.multi_strategy_backtest --from 2026-01-15 --to 2026-04-15

    # S&P 500 daily CSV bars (uses individual_stocks_5yr dataset)
    python -m trading_bot.multi_strategy_backtest --from 2017-02-07 --to 2018-02-07 --daily

Each strategy gets an independent $1,000 virtual allocation. The engine walks
bars chronologically, evaluates each strategy's entry/exit logic per bar,
tracks per-strategy equity curves, and produces a comparison report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, time
from typing import Any, TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from trading_bot.backtest.walkforward import WalkforwardResult

import pandas as pd

from trading_bot.config import Config
from trading_bot.constants import (
    Exchange,
    TICKER_EXCHANGE,
    TZ_EASTERN,
)
from trading_bot.data.fomc_calendar import get_fomc_dates
from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.data_cache import load_cached
from trading_bot.execution.vol_target import vol_target_multiplier
from trading_bot.strategy import calendar_overlay
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.strategies import create_strategies

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal state tracking (defined first — referenced by engine methods)
# ---------------------------------------------------------------------------

@dataclass
class _StrategyState:
    """Mutable state for a single strategy during backtest."""

    strategy: StrategyBase
    cash_usd: float
    initial_cash_usd: float
    peak_equity_usd: float = 0.0
    total_pnl_usd: float = 0.0
    wins: int = 0
    losses: int = 0
    max_drawdown_pct: float = 0.0
    trade_count: int = 0
    open_positions: list[StrategyTrade] = field(default_factory=list)
    closed_trades: list[StrategyTrade] = field(default_factory=list)
    cooldowns: dict[str, datetime] = field(default_factory=dict)
    daily_returns: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity_usd = self.cash_usd


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StrategyTrade:
    """A single simulated trade within a strategy backtest."""

    strategy_id: str
    ticker: str
    exchange: str
    entry_time: datetime
    entry_price: float
    shares: float
    stop_price: float
    target_price: float | None
    trail_pct: float | None
    signals: dict[str, Any]
    hold_type: str = "swing"  # "intraday" or "swing"
    sentiment_score: float | None = None
    trail_activation_pct: float | None = None
    highest_price: float | None = None
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    gross_pnl_usd: float = 0.0
    net_pnl_usd: float = 0.0
    days_held: int = 0


@dataclass
class StrategyResult:
    """Aggregate result for a single strategy over the full backtest period."""

    strategy_id: str
    display_name: str
    initial_cash_usd: float
    final_cash_usd: float
    trades: list[StrategyTrade] = field(default_factory=list)
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl_usd: float = 0.0
    return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = None
    avg_hold_minutes: float = 0.0
    sharpe_approx: float = 0.0
    daily_returns: list[float] = field(default_factory=list)


@dataclass
class MultiStrategyResult:
    """Combined result of all strategies."""

    from_date: str
    to_date: str
    trading_days: int
    strategies: list[StrategyResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class MultiStrategyBacktester:
    """Walks historical bars and evaluates all strategies independently."""

    _DEFAULT_SLIPPAGE_BPS: float = 2.0
    _MAX_SWING_HOLD_DAYS: int = 5

    # Per-ticker slippage: less liquid / wider-spread names get higher slippage
    _TICKER_SLIPPAGE_BPS: dict[str, float] = {
        "F": 1.0,       # Very liquid, tight spread
        "BAC": 1.0,
        "INTC": 1.5,
        "PLTR": 2.0,
        "SOFI": 2.5,
        "AAL": 2.0,
        "SNAP": 2.5,
        "NIO": 3.0,     # Less liquid, wider spread
    }

    def __init__(self, config: Config, calendar_overlay_enabled: bool = True) -> None:
        self._config = config
        # When False, the overlay is bypassed entirely (clean A/B baseline).
        # The default keeps live and backtest in sync — the overlay reads
        # config.calendar_overlay.enabled, which ships False, so this flag
        # only matters when the operator has flipped overlays on.
        self._calendar_overlay_enabled: bool = calendar_overlay_enabled
        self._holiday_calendar: HolidayCalendar = HolidayCalendar(config._raw)
        self._fomc_dates: list[date] = get_fomc_dates()
        self._strategies: list[StrategyBase] = create_strategies(
            config.get_strategy_configs()
        )
        if not self._strategies:
            raise ValueError("No strategies enabled in config")
        logger.info(
            "Loaded %d strategies: %s",
            len(self._strategies),
            [s.strategy_id for s in self._strategies],
        )
        # Vol-target settings live under risk.vol_target in config; if
        # absent, the multiplier collapses to 1.0 (legacy behaviour).
        risk_cfg: dict[str, Any] = self._config._raw.get("risk", {}) or {}
        vt_cfg: dict[str, Any] = risk_cfg.get("vol_target", {}) or {}
        self._vol_target_annual: float = float(
            vt_cfg.get("annual_vol_pct", 0.0)  # 0 disables
        )
        self._vol_target_min_mult: float = float(vt_cfg.get("min_multiplier", 0.5))
        self._vol_target_max_mult: float = float(vt_cfg.get("max_multiplier", 1.5))
        self._vol_target_lookback: int = int(vt_cfg.get("lookback_trades", 30))
        self._vol_target_trades_per_year: int = int(
            vt_cfg.get("trades_per_year", 252)
        )
        if self._vol_target_annual > 0:
            logger.info(
                "Vol target enabled: annual=%.1f%%, lookback=%d trades, "
                "mult bounds [%.2f, %.2f]",
                self._vol_target_annual * 100,
                self._vol_target_lookback,
                self._vol_target_min_mult,
                self._vol_target_max_mult,
            )
        # Regime: optional realized-vol gate on top of the existing
        # SMA50-trend gate. When set, regime_bullish requires *both*
        # close > SMA50 *and* realized vol below the threshold. This
        # catches the 2008/2018-Q4-style crashes that the SMA gate
        # alone misses.
        regime_cfg: dict[str, Any] = risk_cfg.get("regime", {}) or {}
        self._regime_high_vol_threshold: float = float(
            regime_cfg.get("high_vol_threshold_pct", 0.0)
        )  # 0 disables
        self._regime_vol_lookback: int = int(
            regime_cfg.get("vol_lookback_days", 20)
        )
        if self._regime_high_vol_threshold > 0:
            logger.info(
                "Regime high-vol gate enabled: threshold=%.1f%% (annualized), "
                "lookback=%d days",
                self._regime_high_vol_threshold * 100,
                self._regime_vol_lookback,
            )

    def _compute_vol_multiplier(self, st: "_StrategyState") -> Any:
        """Per-strategy vol-target multiplier from recent closed trades.

        Returns a ``VolTargetResult`` whose ``multiplier`` is 1.0 when the
        feature is disabled or insufficient samples exist.
        """
        recent: list[float] = []
        for trade in st.closed_trades[-self._vol_target_lookback:]:
            cost: float = trade.entry_price * float(trade.shares)
            if cost <= 0:
                continue
            recent.append(trade.net_pnl_usd / cost)
        return vol_target_multiplier(
            recent,
            target_annual_vol=self._vol_target_annual,
            expected_trades_per_year=self._vol_target_trades_per_year,
            min_multiplier=self._vol_target_min_mult,
            max_multiplier=self._vol_target_max_mult,
        )

    def _get_tickers(self) -> list[str]:
        raw: dict[str, Any] = self._config._raw.get("watchlist", {})
        tickers: list[str] = list(raw.get("us", []))
        return tickers

    def _resample_to_5min(self, df_1min: pd.DataFrame) -> pd.DataFrame:
        if df_1min.empty:
            return df_1min
        ohlcv = df_1min[["open", "high", "low", "close", "volume"]].copy()
        resampled = ohlcv.resample("5min", closed="left", label="left").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        })
        return resampled.dropna(subset=["open", "close"])

    def _get_slippage_bps(self, ticker: str) -> float:
        return self._TICKER_SLIPPAGE_BPS.get(ticker, self._DEFAULT_SLIPPAGE_BPS)

    def _simulate_fill(self, price: float, side: str, ticker: str = "") -> float:
        bps = self._get_slippage_bps(ticker)
        slippage = price * (bps / 10_000.0)
        return price + slippage if side == "buy" else price - slippage

    def _calendar_overlay_blocks(
        self,
        decision: StrategyDecision,
        bar_dt: datetime,
    ) -> bool:
        """Return True when the overlay vetoes *decision* outright."""
        if not self._calendar_overlay_enabled:
            return False
        try:
            return calendar_overlay.should_block_entry(
                decision,
                bar_dt,
                self._config._raw,
                holiday_calendar=self._holiday_calendar,
            )
        except Exception:
            logger.warning(
                "Calendar overlay block-check failed for %s/%s — passing through",
                decision.strategy_id, decision.ticker, exc_info=True,
            )
            return False

    def _calendar_overlay_multiplier(
        self,
        decision: StrategyDecision,
        bar_dt: datetime,
    ) -> float:
        """Return the composed sizing multiplier (1.0 when overlay disabled)."""
        if not self._calendar_overlay_enabled:
            return 1.0
        try:
            return calendar_overlay.compute_size_multiplier(
                decision,
                bar_dt,
                self._config._raw,
                holiday_calendar=self._holiday_calendar,
                fomc_dates=self._fomc_dates,
            )
        except Exception:
            logger.warning(
                "Calendar overlay multiplier lookup failed for %s/%s — using 1.0",
                decision.strategy_id, decision.ticker, exc_info=True,
            )
            return 1.0

    @staticmethod
    def _compute_sentiment_proxy(df_daily: pd.DataFrame) -> float | None:
        """Derive a sentiment proxy from recent daily price action.

        Uses a blend of:
          - 5-day return momentum
          - 10-day return momentum
          - Recent volatility penalty

        Returns a score roughly in [-1, +1] or None if insufficient data.
        """
        if df_daily is None or len(df_daily) < 12:
            return None

        closes = df_daily["close"].dropna()
        if len(closes) < 12:
            return None

        ret_5 = (float(closes.iloc[-1]) - float(closes.iloc[-6])) / float(closes.iloc[-6])
        ret_10 = (float(closes.iloc[-1]) - float(closes.iloc[-11])) / float(closes.iloc[-11])

        # Volatility: stddev of last 10 daily returns
        daily_rets = closes.pct_change().dropna().iloc[-10:]
        vol = float(daily_rets.std()) if len(daily_rets) >= 5 else 0.02

        # Higher vol reduces the score (noisy moves are less trustworthy)
        vol_penalty = min(vol * 5, 0.3)

        raw_score = (ret_5 * 0.6 + ret_10 * 0.4) * 5.0 - vol_penalty
        return max(-1.0, min(1.0, raw_score))

    @staticmethod
    def _compute_atr(df: pd.DataFrame, period: int = 14) -> float | None:
        """Compute Average True Range from OHLC data.

        Returns the ATR value (in price units) or None if insufficient data.
        """
        if df is None or len(df) < period + 1:
            return None

        high = df["high"].astype(float)
        low = df["low"].astype(float)
        close = df["close"].astype(float)

        prev_close = close.shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)

        atr = float(tr.iloc[-period:].mean())
        return atr if atr > 0 else None

    def _atr_adjusted_stops(
        self,
        entry_price: float,
        df_slice: pd.DataFrame,
        strategy_id: str,
    ) -> tuple[float, float | None, float | None, float]:
        """Compute ATR-based stop, target, trailing pct, and activation pct.

        Returns (stop_price, target_price, trail_pct, trail_activation_pct).
        Stop = 2×ATR, target = 3.5×ATR, trail = 2×ATR (activates after +1.5×ATR).
        """
        atr = self._compute_atr(df_slice)
        if atr is None or entry_price <= 0:
            stop_pct = 0.05
            target_pct = 0.10
            trail_pct = 0.05
            activation_pct = 0.04
        else:
            stop_pct = max(2.0 * atr / entry_price, 0.03)
            target_pct = max(5.0 * atr / entry_price, 0.08)
            trail_pct = max(2.5 * atr / entry_price, 0.04)
            activation_pct = max(2.5 * atr / entry_price, 0.035)

        stop_price = round(entry_price * (1.0 - stop_pct), 2)
        target_price = round(entry_price * (1.0 + target_pct), 2)

        return stop_price, target_price, trail_pct, activation_pct

    @staticmethod
    def _has_excessive_gaps(df: pd.DataFrame, atr: float | None, max_gap_atr: float = 1.5) -> bool:
        """Check if a ticker has average overnight gaps exceeding max_gap_atr × ATR.

        This filters out earnings-prone and volatile names that regularly
        gap through stops on daily bars.
        """
        if df is None or len(df) < 20 or atr is None or atr <= 0:
            return False

        close = df["close"].astype(float)
        open_ = df["open"].astype(float)
        gaps = (open_.iloc[1:].values - close.iloc[:-1].values)
        avg_abs_gap = float(abs(gaps).mean()) if len(gaps) > 0 else 0.0
        return avg_abs_gap > max_gap_atr * atr

    @staticmethod
    def _size_by_risk(
        entry_price: float,
        stop_price: float,
        available_cash: float,
        risk_per_trade_pct: float = 0.02,
        max_position_pct: float = 0.40,
        fractional: bool = True,
        vol_multiplier: float = 1.0,
    ) -> float:
        """Size a position so the max loss equals a fixed % of capital.

        With ``fractional=True`` returns fractional share count (Alpaca supports
        fractional shares down to 1/1000000). With ``fractional=False`` returns
        an integer count.

        ``vol_multiplier`` scales the risk budget (not the cash cap), so a
        strategy whose realized vol is high gets fewer shares but never
        breaks the max-position-pct ceiling.
        """
        risk_dollars = available_cash * risk_per_trade_pct * max(vol_multiplier, 0.0)
        risk_per_share = abs(entry_price - stop_price)
        if risk_per_share <= 0 or entry_price <= 0:
            return 0.0

        shares_by_risk = risk_dollars / risk_per_share
        max_spend = available_cash * max_position_pct
        shares_by_cash = max_spend / entry_price

        shares = max(min(shares_by_risk, shares_by_cash), 0.0)
        if fractional:
            return round(shares, 4)
        return float(int(shares))

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_day_data(
        self, tickers: list[str], target_date: date
    ) -> dict[str, dict[str, pd.DataFrame]]:
        """Load cached intraday + daily data for all tickers on a given day."""
        result: dict[str, dict[str, pd.DataFrame]] = {}
        for ticker in tickers:
            df_1min = load_cached(ticker, target_date, "intraday")
            if df_1min is None or df_1min.empty:
                continue

            df_5min = self._resample_to_5min(df_1min)
            if df_5min.empty:
                continue

            df_daily = load_cached(ticker, target_date, "daily")
            if df_daily is None:
                df_daily = pd.DataFrame()

            result[ticker] = {"intraday": df_5min, "daily": df_daily}

        return result

    # ------------------------------------------------------------------
    # Core run
    # ------------------------------------------------------------------

    async def run(
        self,
        from_date: date,
        to_date: date,
        cash_per_strategy_usd: float = 1000.0,
    ) -> MultiStrategyResult:
        """Run all strategies over the date range.

        SWING positions carry overnight up to ``_MAX_SWING_HOLD_DAYS``.
        Sentiment proxy is computed from daily bars for each ticker.
        """
        trading_days = self._get_trading_days(from_date, to_date)
        tickers = self._get_tickers()

        logger.info(
            "Multi-strategy backtest: %d strategies x %d tickers x %d days",
            len(self._strategies), len(tickers), len(trading_days),
        )

        # Per-strategy state
        states: dict[str, _StrategyState] = {}
        for strat in self._strategies:
            states[strat.strategy_id] = _StrategyState(
                strategy=strat,
                cash_usd=cash_per_strategy_usd,
                initial_cash_usd=cash_per_strategy_usd,
            )

        us_exec_start = time(9, 35)
        us_exec_end = time(15, 50)

        for day_idx, day in enumerate(trading_days):
            day_data = self._load_day_data(tickers, day)
            if not day_data:
                logger.debug("No data for %s — skipping", day)
                # Increment days_held for carried positions even on no-data days
                for st in states.values():
                    for trade in st.open_positions:
                        trade.days_held += 1
                    st.daily_returns.append(0.0)
                continue

            # Pre-compute sentiment proxy per ticker from daily bars
            sentiment_proxies: dict[str, float | None] = {}
            for ticker, data in day_data.items():
                sentiment_proxies[ticker] = self._compute_sentiment_proxy(
                    data.get("daily")
                )

            # Build timeline: (timestamp, ticker, bar_idx)
            events: list[tuple[pd.Timestamp, str, int]] = []
            for ticker, data in day_data.items():
                df_5 = data["intraday"]
                for idx in range(len(df_5)):
                    events.append((df_5.index[idx], ticker, idx))
            events.sort(key=lambda e: e[0])

            # Track daily P&L for each strategy (mark-to-market)
            day_start_equity: dict[str, float] = {}
            for sid, st in states.items():
                pos_value = sum(
                    t.entry_price * t.shares for t in st.open_positions
                )
                day_start_equity[sid] = st.cash_usd + pos_value

            last_bar_per_ticker: dict[str, tuple[pd.Series, datetime]] = {}

            for ts, ticker, bar_idx in events:
                current_time = ts.tz_convert(TZ_EASTERN).to_pydatetime()
                bar_time = current_time.time()
                df_5 = day_data[ticker]["intraday"]
                df_daily = day_data[ticker]["daily"]
                bar = df_5.iloc[bar_idx]
                bar_close = float(bar["close"])
                bar_high = float(bar.get("high", bar_close))
                bar_low = float(bar.get("low", bar_close))
                last_bar_per_ticker[ticker] = (bar, current_time)

                for strat in self._strategies:
                    st = states[strat.strategy_id]

                    # --- Check exits ---
                    for trade in list(st.open_positions):
                        if trade.ticker != ticker:
                            continue
                        exit_signal = self._check_trade_exit(
                            trade, bar_close, bar_high, bar_low, current_time,
                            strat, df_5.iloc[:bar_idx + 1], df_daily,
                        )
                        if exit_signal is not None:
                            reason, exit_px = exit_signal
                            self._close_trade(st, trade, exit_px, reason, current_time)

                    # --- Check entries (execution window only) ---
                    if us_exec_start <= bar_time <= us_exec_end:
                        if bar_idx < 10:
                            continue
                        already_in = any(
                            t.ticker == ticker for t in st.open_positions
                        )
                        if already_in:
                            continue
                        if len(st.open_positions) >= strat.get_max_positions():
                            continue

                        cooldown_until = st.cooldowns.get(ticker)
                        if cooldown_until and current_time < cooldown_until:
                            continue

                        sentiment = sentiment_proxies.get(ticker)
                        decision = strat.evaluate_entry(
                            ticker=ticker,
                            exchange=TICKER_EXCHANGE.get(ticker, Exchange.NYSE).value,
                            df_5min=df_5.iloc[:bar_idx + 1],
                            df_daily=df_daily,
                            current_price=bar_close,
                            available_cash=st.cash_usd,
                            sentiment_score=sentiment,
                        )
                        if decision is not None and decision.shares > 0:
                            if self._calendar_overlay_blocks(decision, current_time):
                                continue
                            ovl_mult = self._calendar_overlay_multiplier(
                                decision, current_time,
                            )
                            if ovl_mult <= 0.0:
                                continue
                            if ovl_mult != 1.0:
                                scaled = max(int(decision.shares * ovl_mult), 0)
                                if scaled < 1:
                                    continue
                                decision = decision.__class__(
                                    **{**decision.__dict__, "shares": scaled}
                                )
                            fill_price = self._simulate_fill(
                                bar_close, "buy", ticker,
                            )
                            cost = fill_price * decision.shares
                            if cost > st.cash_usd:
                                adjusted_shares = int(st.cash_usd / fill_price)
                                if adjusted_shares < 1:
                                    continue
                                decision = decision.__class__(
                                    **{**decision.__dict__, "shares": adjusted_shares}
                                )
                                cost = fill_price * decision.shares

                            trade = StrategyTrade(
                                strategy_id=strat.strategy_id,
                                ticker=ticker,
                                exchange=decision.exchange,
                                entry_time=current_time,
                                entry_price=fill_price,
                                shares=decision.shares,
                                stop_price=decision.stop_price,
                                target_price=decision.target_price,
                                trail_pct=decision.trail_pct,
                                signals=decision.signals,
                                hold_type=decision.hold_type.value,
                                sentiment_score=decision.sentiment_score,
                                highest_price=fill_price,
                            )
                            st.open_positions.append(trade)
                            st.cash_usd -= cost
                            st.trade_count += 1

            # --- End of day ---
            for strat in self._strategies:
                st = states[strat.strategy_id]
                for trade in list(st.open_positions):
                    trade.days_held += 1

                    # Force-close SWING trades that exceeded max hold days
                    if trade.hold_type == "swing" and trade.days_held > self._MAX_SWING_HOLD_DAYS:
                        if trade.ticker in last_bar_per_ticker:
                            last_bar, last_time = last_bar_per_ticker[trade.ticker]
                            close_px = float(last_bar["close"])
                        else:
                            close_px = trade.entry_price
                            last_time = datetime.now(TZ_EASTERN)
                        self._close_trade(st, trade, close_px, "max_hold_days", last_time)
                        continue

                    # Force-close intraday positions at EOD
                    if trade.hold_type == "intraday":
                        if trade.ticker in last_bar_per_ticker:
                            last_bar, last_time = last_bar_per_ticker[trade.ticker]
                            close_px = float(last_bar["close"])
                        else:
                            close_px = trade.entry_price
                            last_time = datetime.now(TZ_EASTERN)
                        self._close_trade(st, trade, close_px, "eod_close", last_time)

            # Record daily return (mark-to-market including open positions)
            for sid, st in states.items():
                pos_value = 0.0
                for trade in st.open_positions:
                    if trade.ticker in last_bar_per_ticker:
                        pos_value += float(last_bar_per_ticker[trade.ticker][0]["close"]) * trade.shares
                    else:
                        pos_value += trade.entry_price * trade.shares
                total_now = st.cash_usd + pos_value
                if total_now > st.peak_equity_usd:
                    st.peak_equity_usd = total_now
                dd_pct = (st.peak_equity_usd - total_now) / st.peak_equity_usd * 100
                if dd_pct > st.max_drawdown_pct:
                    st.max_drawdown_pct = dd_pct
                total_before = day_start_equity[sid]
                if total_before > 0:
                    st.daily_returns.append(
                        (total_now - total_before) / total_before * 100
                    )
                else:
                    st.daily_returns.append(0.0)

            if (day_idx + 1) % 10 == 0:
                carried = sum(
                    len(st.open_positions) for st in states.values()
                )
                logger.info(
                    "Day %d/%d (%s) — %d positions carried overnight",
                    day_idx + 1, len(trading_days), day.isoformat(), carried,
                )

        # Force-close any remaining positions at end of backtest
        for strat in self._strategies:
            st = states[strat.strategy_id]
            for trade in list(st.open_positions):
                self._close_trade(
                    st, trade, trade.entry_price, "backtest_end",
                    datetime(to_date.year, to_date.month, to_date.day,
                             16, 0, 0, tzinfo=TZ_EASTERN),
                )

        # Build results
        strategy_results: list[StrategyResult] = []
        for strat in self._strategies:
            st = states[strat.strategy_id]
            sr = self._build_strategy_result(strat, st)
            strategy_results.append(sr)

        return MultiStrategyResult(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            trading_days=len(trading_days),
            strategies=strategy_results,
        )

    def _check_trade_exit(
        self,
        trade: StrategyTrade,
        bar_close: float,
        bar_high: float,
        bar_low: float,
        current_time: datetime,
        strategy: StrategyBase,
        df_5min: pd.DataFrame,
        df_daily: pd.DataFrame,
    ) -> tuple[str, float] | None:
        """Check if a trade should exit. Returns (reason, exit_price) or None."""
        # Update highest price
        if trade.highest_price is None or bar_high > trade.highest_price:
            trade.highest_price = bar_high

        # Stop loss (intrabar check using bar low)
        if trade.stop_price and bar_low <= trade.stop_price:
            return "stop_loss", min(trade.stop_price, bar_close)

        # Take profit (intrabar check using bar high)
        if trade.target_price and bar_high >= trade.target_price:
            return "take_profit", trade.target_price

        # Trailing stop (only after activation threshold is reached)
        if trade.trail_pct and trade.highest_price:
            activation_pct = trade.trail_activation_pct or 0.0
            up_pct = (trade.highest_price - trade.entry_price) / trade.entry_price
            if up_pct >= activation_pct:
                trail_stop = trade.highest_price * (1.0 - trade.trail_pct)
                if bar_low <= trail_stop:
                    return "trailing_stop", min(trail_stop, bar_close)

        # Delegate to strategy's own evaluate_exit
        position_dict: dict[str, Any] = {
            "entry_price": trade.entry_price,
            "stop_price": trade.stop_price,
            "highest_price": trade.highest_price,
            "target_price": trade.target_price,
            "entry_time": trade.entry_time,
            "hold_type": trade.hold_type,
        }
        exit_signal: ExitSignal = strategy.evaluate_exit(
            position=position_dict,
            current_price=bar_close,
            df_5min=df_5min,
            df_daily=df_daily,
        )
        if exit_signal.should_exit:
            return exit_signal.reason or "strategy_exit", bar_close

        return None

    def _close_trade(
        self,
        state: _StrategyState,
        trade: StrategyTrade,
        exit_price: float,
        reason: str,
        exit_time: datetime,
    ) -> None:
        fill_exit = self._simulate_fill(exit_price, "sell", trade.ticker)
        gross_pnl = (fill_exit - trade.entry_price) * trade.shares

        trade.exit_time = exit_time
        trade.exit_price = fill_exit
        trade.exit_reason = reason
        trade.gross_pnl_usd = gross_pnl
        trade.net_pnl_usd = gross_pnl  # commission-free

        state.cash_usd += fill_exit * trade.shares
        state.total_pnl_usd += gross_pnl
        if gross_pnl > 0:
            state.wins += 1
        else:
            state.losses += 1

        # Peak equity and drawdown are tracked via mark-to-market in the
        # daily loop (cash + open-position value), not on close — closing a
        # position only moves the same equity from "open" into "cash".
        state.cooldowns[trade.ticker] = exit_time + timedelta(minutes=30)
        state.open_positions.remove(trade)
        state.closed_trades.append(trade)

    def _build_strategy_result(
        self, strategy: StrategyBase, state: _StrategyState
    ) -> StrategyResult:
        trades = state.closed_trades
        total = len(trades)
        wins = state.wins
        losses = state.losses

        total_pnl = sum(t.net_pnl_usd for t in trades)
        return_pct = (total_pnl / state.initial_cash_usd * 100) if state.initial_cash_usd > 0 else 0

        win_pnl = sum(t.net_pnl_usd for t in trades if t.net_pnl_usd > 0)
        loss_pnl = abs(sum(t.net_pnl_usd for t in trades if t.net_pnl_usd < 0))
        profit_factor = (win_pnl / loss_pnl) if loss_pnl > 0 else None

        hold_mins: list[float] = []
        for t in trades:
            if t.exit_time:
                hold_mins.append((t.exit_time - t.entry_time).total_seconds() / 60)
        avg_hold = statistics.mean(hold_mins) if hold_mins else 0.0

        avg_daily = statistics.mean(state.daily_returns) if state.daily_returns else 0.0
        std_daily = statistics.stdev(state.daily_returns) if len(state.daily_returns) > 1 else 0.0
        sharpe = (avg_daily / std_daily) * (252 ** 0.5) if std_daily > 0 else 0.0

        return StrategyResult(
            strategy_id=strategy.strategy_id,
            display_name=strategy.display_name,
            initial_cash_usd=state.initial_cash_usd,
            final_cash_usd=state.cash_usd,
            trades=trades,
            total_trades=total,
            wins=wins,
            losses=losses,
            total_pnl_usd=total_pnl,
            return_pct=return_pct,
            max_drawdown_pct=state.max_drawdown_pct,
            win_rate=(wins / total * 100) if total > 0 else 0.0,
            profit_factor=profit_factor,
            avg_hold_minutes=avg_hold,
            sharpe_approx=sharpe,
            daily_returns=state.daily_returns,
        )

    def _get_trading_days(self, start: date, end: date) -> list[date]:
        holidays_raw: dict[str, Any] = self._config._raw.get("holidays", {})
        holiday_dates: set[str] = set()
        for val in holidays_raw.values():
            if isinstance(val, list):
                for h in val:
                    holiday_dates.add(str(h))

        days: list[date] = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d.isoformat() not in holiday_dates:
                days.append(d)
            d += timedelta(days=1)
        return days

    # ------------------------------------------------------------------
    # Daily-bar mode (S&P 500 CSV dataset)
    # ------------------------------------------------------------------

    @staticmethod
    def _build_market_index(
        universe: dict[str, pd.DataFrame],
        sma_period: int = 50,
    ) -> pd.DataFrame:
        """Build a synthetic equal-weight market index from the universe.

        Returns a DataFrame with columns: close, sma, regime_bullish.
        """
        close_frames: list[pd.Series] = []
        for ticker, df in universe.items():
            close_frames.append(df["close"].rename(ticker))

        if not close_frames:
            return pd.DataFrame()

        panel = pd.concat(close_frames, axis=1)
        daily_returns = panel.pct_change()
        avg_return = daily_returns.mean(axis=1)
        index_level = (1 + avg_return).cumprod() * 100
        index_level.iloc[0] = 100.0

        result = pd.DataFrame({"close": index_level})
        result["sma"] = result["close"].rolling(sma_period, min_periods=sma_period).mean()
        result["regime_bullish"] = result["close"] > result["sma"]
        return result

    async def run_daily(
        self,
        from_date: date,
        to_date: date,
        cash_per_strategy_usd: float = 1000.0,
        min_avg_volume: int = 1_000_000,
        min_price: float = 5.0,
        max_price: float = 500.0,
        regime_filter: bool = True,
    ) -> MultiStrategyResult:
        """Run strategies on daily bars from the S&P 500 CSV dataset.

        Instead of walking intraday bars, this evaluates each ticker once
        per trading day using the daily close. Strategies receive daily bars
        as both ``df_5min`` and ``df_daily`` — the indicator math is
        timeframe-agnostic.

        If ``regime_filter`` is True, new entries are only allowed when the
        synthetic equal-weight market index is above its 50-day SMA.
        """
        from trading_bot.data.sp500_loader import load_universe

        universe = load_universe(
            from_date, to_date,
            min_avg_volume=min_avg_volume,
            min_price=min_price,
            max_price=max_price,
        )
        if not universe:
            logger.error("No tickers passed filters — nothing to backtest")
            return MultiStrategyResult(
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
                trading_days=0,
                strategies=[],
            )

        # Build sorted list of all trading days present in any ticker
        all_dates: set[date] = set()
        for df in universe.values():
            for ts in df.index:
                d = ts.date() if hasattr(ts, "date") else ts
                if from_date <= d <= to_date:
                    all_dates.add(d)
        trading_days: list[date] = sorted(all_dates)

        # Market regime filter: synthetic equal-weight index vs 50-day SMA,
        # optionally AND-gated on realized vol.
        market_index: pd.DataFrame = pd.DataFrame()
        if regime_filter:
            market_index = self._build_market_index(universe, sma_period=50)
            if (
                not market_index.empty
                and self._regime_high_vol_threshold > 0
            ):
                ret = market_index["close"].pct_change()
                realized = (
                    ret.rolling(self._regime_vol_lookback,
                                min_periods=self._regime_vol_lookback).std()
                    * math.sqrt(252)
                )
                market_index["realized_vol"] = realized
                vol_gate = realized < self._regime_high_vol_threshold
                market_index["regime_bullish"] = (
                    market_index["regime_bullish"] & vol_gate.fillna(True)
                )
            bullish_days = int(market_index["regime_bullish"].sum()) if not market_index.empty else 0
            total_idx_days = len(market_index.dropna(subset=["sma"]))
            logger.info(
                "Market regime filter: %d/%d days bullish (index > 50-day SMA, "
                "vol_threshold=%s)",
                bullish_days, total_idx_days,
                f"{self._regime_high_vol_threshold * 100:.1f}%"
                if self._regime_high_vol_threshold > 0 else "off",
            )

        logger.info(
            "Daily-bar backtest: %d strategies x %d tickers x %d days (regime_filter=%s)",
            len(self._strategies), len(universe), len(trading_days), regime_filter,
        )

        # Per-strategy state
        states: dict[str, _StrategyState] = {}
        for strat in self._strategies:
            states[strat.strategy_id] = _StrategyState(
                strategy=strat,
                cash_usd=cash_per_strategy_usd,
                initial_cash_usd=cash_per_strategy_usd,
            )

        # Lookback window: how many prior daily bars to pass to strategies
        LOOKBACK: int = 60
        MAX_HOLD_DAYS: int = 15  # wider than intraday mode (3 trading weeks)
        entries_blocked_by_regime: int = 0

        for day_idx, day in enumerate(trading_days):
            day_ts = pd.Timestamp(day)

            # Check market regime for this day
            day_is_bullish: bool = True
            if regime_filter and not market_index.empty:
                if day_ts in market_index.index:
                    val = market_index.loc[day_ts, "regime_bullish"]
                    day_is_bullish = bool(val) if not pd.isna(val) else True
                else:
                    day_is_bullish = True

            # Mark-to-market at start of day
            day_start_equity: dict[str, float] = {}
            for sid, st in states.items():
                pos_value = sum(
                    t.entry_price * t.shares for t in st.open_positions
                )
                day_start_equity[sid] = st.cash_usd + pos_value

            # Iterate all tickers that have data for this day
            for ticker, full_df in universe.items():
                if day_ts not in full_df.index:
                    continue

                # Slice up to and including today
                loc = full_df.index.get_loc(day_ts)
                if isinstance(loc, slice):
                    idx = loc.stop - 1
                else:
                    idx = loc
                start_idx = max(0, idx - LOOKBACK + 1)
                df_slice = full_df.iloc[start_idx:idx + 1]

                if len(df_slice) < 20:
                    continue

                bar = full_df.iloc[idx]
                bar_close = float(bar["close"])
                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                current_time = datetime(
                    day.year, day.month, day.day, 16, 0, 0,
                    tzinfo=TZ_EASTERN,
                )

                sentiment = self._compute_sentiment_proxy(df_slice)

                for strat in self._strategies:
                    st = states[strat.strategy_id]

                    # --- Check exits ---
                    for trade in list(st.open_positions):
                        if trade.ticker != ticker:
                            continue
                        exit_signal = self._check_trade_exit(
                            trade, bar_close, bar_high, bar_low,
                            current_time, strat, df_slice, df_slice,
                        )
                        if exit_signal is not None:
                            reason, exit_px = exit_signal
                            self._close_trade(
                                st, trade, exit_px, reason, current_time,
                            )

                    # --- Check entries ---
                    if not day_is_bullish:
                        entries_blocked_by_regime += 1
                        continue
                    already_in = any(
                        t.ticker == ticker for t in st.open_positions
                    )
                    if already_in:
                        continue
                    if len(st.open_positions) >= strat.get_max_positions():
                        continue

                    cooldown_until = st.cooldowns.get(ticker)
                    if cooldown_until and current_time < cooldown_until:
                        continue

                    # Skip tickers with excessive overnight gaps
                    ticker_atr = self._compute_atr(df_slice)
                    if self._has_excessive_gaps(df_slice, ticker_atr):
                        continue

                    decision = strat.evaluate_entry(
                        ticker=ticker,
                        exchange="NYSE",
                        df_5min=df_slice,
                        df_daily=df_slice,
                        current_price=bar_close,
                        available_cash=st.cash_usd,
                        sentiment_score=sentiment,
                    )
                    if decision is not None and decision.shares > 0:
                        if self._calendar_overlay_blocks(decision, current_time):
                            continue
                        ovl_mult = self._calendar_overlay_multiplier(
                            decision, current_time,
                        )
                        if ovl_mult <= 0.0:
                            continue
                        fill_price = self._simulate_fill(
                            bar_close, "buy", ticker,
                        )

                        # Override stops/targets with ATR-based values
                        atr_stop, atr_target, atr_trail, atr_activation = self._atr_adjusted_stops(
                            fill_price, df_slice, strat.strategy_id,
                        )

                        # ATR-risk-based position sizing: risk 2% of current equity per trade
                        equity = st.cash_usd + sum(
                            t.entry_price * t.shares for t in st.open_positions
                        )
                        vt = self._compute_vol_multiplier(st)
                        shares = self._size_by_risk(
                            fill_price, atr_stop, equity,
                            risk_per_trade_pct=0.02,
                            max_position_pct=0.40,
                            fractional=False,
                            vol_multiplier=vt.multiplier,
                        )
                        if ovl_mult != 1.0:
                            shares = float(int(shares * ovl_mult))
                        if shares < 1:
                            continue

                        cost = fill_price * shares
                        if cost > st.cash_usd:
                            shares = float(int(st.cash_usd / fill_price))
                            if shares < 1:
                                continue
                            cost = fill_price * shares

                        trade = StrategyTrade(
                            strategy_id=strat.strategy_id,
                            ticker=ticker,
                            exchange="NYSE",
                            entry_time=current_time,
                            entry_price=fill_price,
                            shares=shares,
                            stop_price=atr_stop,
                            target_price=atr_target,
                            trail_pct=atr_trail,
                            signals={**decision.signals, "atr_stop": atr_stop, "atr_target": atr_target},
                            hold_type=decision.hold_type.value,
                            sentiment_score=decision.sentiment_score,
                            trail_activation_pct=atr_activation,
                            highest_price=fill_price,
                        )
                        st.open_positions.append(trade)
                        st.cash_usd -= cost
                        st.trade_count += 1

            # --- End of day: increment hold counters, close expired ---
            for strat in self._strategies:
                st = states[strat.strategy_id]
                for trade in list(st.open_positions):
                    trade.days_held += 1
                    if trade.days_held > MAX_HOLD_DAYS:
                        eod_time = datetime(
                            day.year, day.month, day.day, 16, 0, 0,
                            tzinfo=TZ_EASTERN,
                        )
                        if day_ts in universe.get(trade.ticker, pd.DataFrame()).index:
                            px = float(universe[trade.ticker].loc[day_ts, "close"])
                        else:
                            px = trade.entry_price
                        self._close_trade(
                            st, trade, px, "max_hold_days", eod_time,
                        )

            # Mark-to-market daily returns
            for sid, st in states.items():
                pos_value = 0.0
                for trade in st.open_positions:
                    if day_ts in universe.get(trade.ticker, pd.DataFrame()).index:
                        pos_value += float(universe[trade.ticker].loc[day_ts, "close"]) * trade.shares
                    else:
                        pos_value += trade.entry_price * trade.shares
                total_now = st.cash_usd + pos_value
                if total_now > st.peak_equity_usd:
                    st.peak_equity_usd = total_now
                dd_pct = (st.peak_equity_usd - total_now) / st.peak_equity_usd * 100
                if dd_pct > st.max_drawdown_pct:
                    st.max_drawdown_pct = dd_pct
                total_before = day_start_equity[sid]
                if total_before > 0:
                    st.daily_returns.append(
                        (total_now - total_before) / total_before * 100
                    )
                else:
                    st.daily_returns.append(0.0)

            if (day_idx + 1) % 50 == 0:
                carried = sum(len(st.open_positions) for st in states.values())
                logger.info(
                    "Day %d/%d (%s) — %d open positions across all strategies",
                    day_idx + 1, len(trading_days), day.isoformat(), carried,
                )

        if regime_filter:
            logger.info(
                "Regime filter blocked %d potential entry evaluations",
                entries_blocked_by_regime,
            )

        # Force-close remaining positions
        for strat in self._strategies:
            st = states[strat.strategy_id]
            for trade in list(st.open_positions):
                end_time = datetime(
                    to_date.year, to_date.month, to_date.day,
                    16, 0, 0, tzinfo=TZ_EASTERN,
                )
                self._close_trade(
                    st, trade, trade.entry_price, "backtest_end", end_time,
                )

        strategy_results: list[StrategyResult] = []
        for strat in self._strategies:
            st = states[strat.strategy_id]
            sr = self._build_strategy_result(strat, st)
            strategy_results.append(sr)

        return MultiStrategyResult(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            trading_days=len(trading_days),
            strategies=strategy_results,
        )

    # ------------------------------------------------------------------
    # SPY intraday mode (1-min CSV resampled to 5-min)
    # ------------------------------------------------------------------

    async def run_spy_intraday(
        self,
        from_date: date,
        to_date: date,
        cash_per_strategy_usd: float = 1000.0,
        regime_filter: bool = True,
    ) -> MultiStrategyResult:
        """Run strategies on 5-min SPY bars with precise intraday execution.

        Uses the 1-minute SPY CSV dataset, resampled to 5-minute bars.
        Single ticker (SPY) but with intrabar stop execution — no gap-through.
        """
        from trading_bot.data.spy_intraday_loader import load_spy_range, get_trading_days

        trading_days = get_trading_days(from_date, to_date)
        if not trading_days:
            logger.error("No SPY trading days in range")
            return MultiStrategyResult(
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
                trading_days=0,
                strategies=[],
            )

        # Load full range of 5-min bars
        df_5min = load_spy_range(from_date, to_date, resample="5min")
        if df_5min.empty:
            logger.error("No SPY 5-min data loaded")
            return MultiStrategyResult(
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
                trading_days=0,
                strategies=[],
            )

        # Build daily bars from 5-min data for indicators / sentiment proxy
        df_daily = df_5min.resample("1D").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }).dropna(subset=["close"])

        # Market regime: SPY close > 50-day SMA, optionally AND-gated on
        # realized vol (annualized stdev of daily returns) being below a
        # configured threshold.
        df_daily["sma50"] = df_daily["close"].rolling(50, min_periods=50).mean()
        sma_gate: pd.Series = df_daily["close"] > df_daily["sma50"]
        if self._regime_high_vol_threshold > 0:
            daily_ret: pd.Series = df_daily["close"].pct_change()
            realized_ann_vol: pd.Series = (
                daily_ret.rolling(self._regime_vol_lookback,
                                  min_periods=self._regime_vol_lookback).std()
                * math.sqrt(252)
            )
            df_daily["realized_vol"] = realized_ann_vol
            vol_gate: pd.Series = realized_ann_vol < self._regime_high_vol_threshold
            df_daily["regime_bullish"] = sma_gate & vol_gate.fillna(True)
        else:
            df_daily["regime_bullish"] = sma_gate

        logger.info(
            "SPY intraday backtest: %d strategies x %d 5-min bars x %d days (regime=%s)",
            len(self._strategies), len(df_5min), len(trading_days), regime_filter,
        )

        states: dict[str, _StrategyState] = {}
        for strat in self._strategies:
            states[strat.strategy_id] = _StrategyState(
                strategy=strat,
                cash_usd=cash_per_strategy_usd,
                initial_cash_usd=cash_per_strategy_usd,
            )

        EXEC_START = time(9, 35)
        EXEC_END = time(15, 50)
        WIND_DOWN = time(15, 50)
        LOOKBACK_BARS: int = 80
        entries_blocked: int = 0
        ticker = "SPY"

        prev_day: date | None = None
        # Initialize before the loop so the first iteration's
        # ``day_start_equity.get(sid, total_now)`` read in the
        # ``prev_day is not None`` branch can never be unbound. ruff
        # flagged this as F821 along the path where the inner branch
        # could in theory reach a not-yet-assigned variable; in practice
        # ``prev_day is None`` on the very first bar prevents the read,
        # but explicit init removes the smell.
        day_start_equity: dict[str, float] = {}

        for bar_idx in range(LOOKBACK_BARS, len(df_5min)):
            bar = df_5min.iloc[bar_idx]
            bar_ts = df_5min.index[bar_idx]
            bar_dt = bar_ts.to_pydatetime() if hasattr(bar_ts, "to_pydatetime") else bar_ts
            if bar_dt.tzinfo is None:
                bar_dt = bar_dt.replace(tzinfo=TZ_EASTERN)
            bar_time = bar_dt.time()
            bar_date = bar_dt.date()
            bar_close = float(bar["close"])
            bar_high = float(bar["high"])
            bar_low = float(bar["low"])

            # New day: mark-to-market, increment hold counters
            if bar_date != prev_day:
                if prev_day is not None:
                    for sid, st in states.items():
                        pos_value = sum(
                            t.entry_price * t.shares for t in st.open_positions
                        )
                        total_now = st.cash_usd + pos_value
                        total_before = day_start_equity.get(sid, total_now)
                        if total_before > 0:
                            st.daily_returns.append(
                                (total_now - total_before) / total_before * 100
                            )
                        else:
                            st.daily_returns.append(0.0)

                    for st in states.values():
                        for trade in st.open_positions:
                            trade.days_held += 1

                day_start_equity = {}
                for sid, st in states.items():
                    pos_value = sum(
                        t.entry_price * t.shares for t in st.open_positions
                    )
                    day_start_equity[sid] = st.cash_usd + pos_value

                # Check regime for this day
                day_bullish: bool = True
                if regime_filter:
                    day_ts = pd.Timestamp(bar_date)
                    if day_ts in df_daily.index:
                        val = df_daily.loc[day_ts, "regime_bullish"]
                        day_bullish = bool(val) if not pd.isna(val) else True

                prev_day = bar_date

            # Skip pre-market and post-market
            if bar_time < EXEC_START or bar_time > EXEC_END:
                continue

            # Lookback slice for indicators
            df_slice = df_5min.iloc[bar_idx - LOOKBACK_BARS + 1 : bar_idx + 1]

            # Daily lookback for sentiment
            daily_slice = df_daily.loc[:pd.Timestamp(bar_date)]
            daily_slice = daily_slice.iloc[-60:] if len(daily_slice) > 60 else daily_slice
            sentiment = self._compute_sentiment_proxy(daily_slice)

            for strat in self._strategies:
                st = states[strat.strategy_id]

                # --- Exits (always check, even in wind-down) ---
                for trade in list(st.open_positions):
                    exit_signal = self._check_trade_exit(
                        trade, bar_close, bar_high, bar_low,
                        bar_dt, strat, df_slice, daily_slice,
                    )
                    if exit_signal is not None:
                        reason, exit_px = exit_signal
                        self._close_trade(st, trade, exit_px, reason, bar_dt)

                # --- Close INTRADAY positions at wind-down ---
                if bar_time >= WIND_DOWN:
                    for trade in list(st.open_positions):
                        if trade.hold_type == "intraday":
                            self._close_trade(st, trade, bar_close, "eod_close", bar_dt)
                    continue

                # --- Entries ---
                if not day_bullish:
                    entries_blocked += 1
                    continue
                if len(st.open_positions) >= strat.get_max_positions():
                    continue

                already_in = any(t.ticker == ticker for t in st.open_positions)
                if already_in:
                    continue

                cooldown_until = st.cooldowns.get(ticker)
                if cooldown_until and bar_dt < cooldown_until:
                    continue

                decision = strat.evaluate_entry(
                    ticker=ticker,
                    exchange="NYSE",
                    df_5min=df_slice,
                    df_daily=daily_slice,
                    current_price=bar_close,
                    available_cash=st.cash_usd,
                    sentiment_score=sentiment,
                )
                if decision is not None and decision.shares > 0:
                    if self._calendar_overlay_blocks(decision, bar_dt):
                        continue
                    ovl_mult = self._calendar_overlay_multiplier(decision, bar_dt)
                    if ovl_mult <= 0.0:
                        continue
                    fill_price = self._simulate_fill(bar_close, "buy", ticker)

                    # Use 5-min ATR for intraday stop/target sizing
                    atr_stop, atr_target, atr_trail, atr_activation = self._atr_adjusted_stops(
                        fill_price, df_slice, strat.strategy_id,
                    )

                    # Honour "let winners run" signal from the strategy
                    # (decision.target_price is None). Promote the ATR target
                    # to the trailing-stop activation trigger; no hard target.
                    if decision.target_price is None:
                        effective_target: float | None = None
                        effective_activation_pct = max(
                            (atr_target - fill_price) / fill_price, 0.0
                        ) if fill_price > 0 else atr_activation
                    else:
                        effective_target = atr_target
                        effective_activation_pct = atr_activation

                    equity = st.cash_usd + sum(
                        t.entry_price * t.shares for t in st.open_positions
                    )
                    vt = self._compute_vol_multiplier(st)
                    shares = self._size_by_risk(
                        fill_price, atr_stop, equity,
                        risk_per_trade_pct=0.03,
                        max_position_pct=0.90,
                        fractional=True,
                        vol_multiplier=vt.multiplier,
                    )
                    if ovl_mult != 1.0:
                        shares = round(shares * ovl_mult, 4)
                    if shares <= 0.001:
                        continue

                    cost = fill_price * shares
                    if cost > st.cash_usd:
                        shares = round(st.cash_usd / fill_price, 4)
                        if shares <= 0.001:
                            continue
                        cost = fill_price * shares

                    trade = StrategyTrade(
                        strategy_id=strat.strategy_id,
                        ticker=ticker,
                        exchange="NYSE",
                        entry_time=bar_dt,
                        entry_price=fill_price,
                        shares=shares,
                        stop_price=atr_stop,
                        target_price=effective_target,
                        trail_pct=atr_trail,
                        signals={**decision.signals, "atr_stop": atr_stop, "atr_target": atr_target},
                        hold_type=decision.hold_type.value,
                        sentiment_score=decision.sentiment_score,
                        trail_activation_pct=effective_activation_pct,
                        highest_price=fill_price,
                    )
                    st.open_positions.append(trade)
                    st.cash_usd -= cost
                    st.trade_count += 1

            # Progress logging
            if bar_idx % 10000 == 0:
                carried = sum(len(st.open_positions) for st in states.values())
                logger.info(
                    "Bar %d/%d (%s) — %d open positions",
                    bar_idx, len(df_5min), bar_date.isoformat(), carried,
                )

        # Force-close remaining
        for strat in self._strategies:
            st = states[strat.strategy_id]
            for trade in list(st.open_positions):
                end_time = datetime(
                    to_date.year, to_date.month, to_date.day,
                    16, 0, 0, tzinfo=TZ_EASTERN,
                )
                last_close = float(df_5min.iloc[-1]["close"])
                self._close_trade(st, trade, last_close, "backtest_end", end_time)

        if regime_filter:
            logger.info("Regime filter blocked %d entry evaluations", entries_blocked)

        strategy_results: list[StrategyResult] = []
        for strat in self._strategies:
            st = states[strat.strategy_id]
            sr = self._build_strategy_result(strat, st)
            strategy_results.append(sr)

        return MultiStrategyResult(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            trading_days=len(trading_days),
            strategies=strategy_results,
        )

    # ------------------------------------------------------------------
    # Multi-ticker intraday mode (Alpaca 1-min cache, resampled to 5-min)
    # ------------------------------------------------------------------

    async def run_multi_ticker_intraday(
        self,
        from_date: date,
        to_date: date,
        tickers: list[str],
        cash_per_strategy_usd: float = 1000.0,
        regime_filter: bool = True,
        regime_ticker: str = "SPY",
        regime_sma_period: int | None = None,
    ) -> MultiStrategyResult:
        """Run strategies on 5-min bars across multiple tickers.

        Loads 1-min bars from the parquet cache per (ticker, day), resamples
        to 5-min, and walks bars chronologically. Same intrabar stop execution
        as ``run_spy_intraday`` but across a basket of tickers.
        """
        from trading_bot.data_cache import load_cached

        trading_days: list[date] = self._get_trading_days(from_date, to_date)
        if not trading_days:
            return MultiStrategyResult(
                from_date=from_date.isoformat(),
                to_date=to_date.isoformat(),
                trading_days=0,
                strategies=[],
            )

        logger.info(
            "Multi-ticker intraday: %d strategies x %d tickers x %d days (regime=%s)",
            len(self._strategies), len(tickers), len(trading_days), regime_filter,
        )

        # Build per-ticker daily history by aggregating the 1-min cache we
        # already loaded. This gives us daily bars covering the entire backtest
        # range without depending on the short cached-daily files.
        per_ticker_daily: dict[str, pd.DataFrame] = {}
        for ticker in tickers:
            daily_chunks: list[pd.DataFrame] = []
            for day in trading_days:
                df_1min = load_cached(ticker, day, "intraday")
                if df_1min is None or df_1min.empty:
                    continue
                closes = df_1min["close"]
                daily_chunks.append(pd.DataFrame({
                    "open":   [float(df_1min["open"].iloc[0])],
                    "high":   [float(df_1min["high"].max())],
                    "low":    [float(df_1min["low"].min())],
                    "close":  [float(closes.iloc[-1])],
                    "volume": [int(df_1min["volume"].sum())],
                }, index=pd.DatetimeIndex([pd.Timestamp(day)], name="timestamp")))
            if daily_chunks:
                per_ticker_daily[ticker] = pd.concat(daily_chunks).sort_index()

        # Market regime from regime_ticker's daily bars.
        # Index is tz-naive date-keyed to avoid tz mismatches in lookups.
        market_regime: pd.DataFrame = pd.DataFrame()
        if regime_filter and regime_ticker in per_ticker_daily:
            # Resolve SMA period: explicit arg > config > default 50
            sma_p: int = regime_sma_period if regime_sma_period is not None else int(
                self._config._raw.get("multi_strategy", {}).get(
                    "regime_filter", {}
                ).get("sma_period", 50)
            )
            df = per_ticker_daily[regime_ticker].copy()
            df["sma50"] = df["close"].rolling(sma_p, min_periods=sma_p).mean()
            sma_gate = df["close"] > df["sma50"]
            if self._regime_high_vol_threshold > 0:
                ret = df["close"].pct_change()
                realized = (
                    ret.rolling(self._regime_vol_lookback,
                                min_periods=self._regime_vol_lookback).std()
                    * math.sqrt(252)
                )
                df["realized_vol"] = realized
                vol_gate = realized < self._regime_high_vol_threshold
                df["regime_bullish"] = sma_gate & vol_gate.fillna(True)
            else:
                df["regime_bullish"] = sma_gate
            market_regime = df
            bullish_days = int(df["regime_bullish"].fillna(False).sum())
            total_rated = int(df["sma50"].notna().sum())
            logger.info(
                "Regime filter calibrated on %s: %d/%d days bullish (close > %d-day SMA, "
                "vol_threshold=%s)",
                regime_ticker, bullish_days, total_rated, sma_p,
                f"{self._regime_high_vol_threshold * 100:.1f}%"
                if self._regime_high_vol_threshold > 0 else "off",
            )

        states: dict[str, _StrategyState] = {}
        for strat in self._strategies:
            states[strat.strategy_id] = _StrategyState(
                strategy=strat,
                cash_usd=cash_per_strategy_usd,
                initial_cash_usd=cash_per_strategy_usd,
            )

        EXEC_START = time(9, 35)
        EXEC_END = time(15, 50)
        WIND_DOWN = time(15, 50)
        LOOKBACK_BARS: int = 80

        # Rolling 5-min history per ticker across days
        rolling_5min: dict[str, pd.DataFrame] = {t: pd.DataFrame() for t in tickers}
        entries_blocked: int = 0

        for day_idx, day in enumerate(trading_days):
            # Load each ticker's 1-min bars for this day, resample to 5-min
            day_bars: dict[str, pd.DataFrame] = {}
            for ticker in tickers:
                df_1min = load_cached(ticker, day, "intraday")
                if df_1min is None or df_1min.empty:
                    continue
                df_5 = self._resample_to_5min(df_1min)
                if df_5.empty:
                    continue
                day_bars[ticker] = df_5

            if not day_bars:
                for st in states.values():
                    for trade in st.open_positions:
                        trade.days_held += 1
                    st.daily_returns.append(0.0)
                continue

            # Day regime (match by date to avoid tz-aware/naive mismatches)
            day_bullish: bool = True
            if regime_filter and not market_regime.empty:
                matches = market_regime.loc[
                    market_regime.index.normalize() == pd.Timestamp(day)
                ]
                if not matches.empty:
                    val = matches.iloc[-1].get("regime_bullish")
                    day_bullish = bool(val) if not pd.isna(val) else True

            day_start_equity: dict[str, float] = {}
            for sid, st in states.items():
                pos_value = sum(
                    t.entry_price * t.shares for t in st.open_positions
                )
                day_start_equity[sid] = st.cash_usd + pos_value

            # Build unified timeline of (timestamp, ticker) events sorted
            events: list[tuple[pd.Timestamp, str]] = []
            for ticker, df_5 in day_bars.items():
                for ts in df_5.index:
                    events.append((ts, ticker))
            events.sort(key=lambda e: e[0])

            for ts, ticker in events:
                bar_dt = ts.tz_convert(TZ_EASTERN).to_pydatetime() if hasattr(ts, "tz_convert") else ts
                bar_time = bar_dt.time()
                df_5 = day_bars[ticker]
                bar = df_5.loc[ts]
                bar_close = float(bar["close"])
                bar_high = float(bar["high"])
                bar_low = float(bar["low"])

                # Update rolling history for this ticker
                existing = rolling_5min.get(ticker, pd.DataFrame())
                new_row = df_5.loc[[ts]]
                rolling_5min[ticker] = pd.concat([existing, new_row]).tail(LOOKBACK_BARS * 2)

                df_slice = rolling_5min[ticker].tail(LOOKBACK_BARS)
                if len(df_slice) < 30:
                    continue

                daily_slice = per_ticker_daily.get(ticker, pd.DataFrame())
                sentiment = self._compute_sentiment_proxy(daily_slice) if not daily_slice.empty else None

                for strat in self._strategies:
                    st = states[strat.strategy_id]

                    # Exits for this ticker's open positions
                    for trade in list(st.open_positions):
                        if trade.ticker != ticker:
                            continue
                        exit_signal = self._check_trade_exit(
                            trade, bar_close, bar_high, bar_low,
                            bar_dt, strat, df_slice, daily_slice,
                        )
                        if exit_signal is not None:
                            reason, exit_px = exit_signal
                            self._close_trade(st, trade, exit_px, reason, bar_dt)

                    # Intraday close at wind-down
                    if bar_time >= WIND_DOWN:
                        for trade in list(st.open_positions):
                            if trade.ticker == ticker and trade.hold_type == "intraday":
                                self._close_trade(st, trade, bar_close, "eod_close", bar_dt)
                        continue

                    # Entries
                    if bar_time < EXEC_START:
                        continue
                    if not day_bullish:
                        entries_blocked += 1
                        continue
                    if len(st.open_positions) >= strat.get_max_positions():
                        continue
                    if any(t.ticker == ticker for t in st.open_positions):
                        continue
                    cooldown_until = st.cooldowns.get(ticker)
                    if cooldown_until and bar_dt < cooldown_until:
                        continue

                    decision = strat.evaluate_entry(
                        ticker=ticker,
                        exchange="NYSE",
                        df_5min=df_slice,
                        df_daily=daily_slice if not daily_slice.empty else df_slice,
                        current_price=bar_close,
                        available_cash=st.cash_usd,
                        sentiment_score=sentiment,
                    )
                    if decision is None or decision.shares <= 0:
                        continue

                    if self._calendar_overlay_blocks(decision, bar_dt):
                        continue
                    ovl_mult = self._calendar_overlay_multiplier(decision, bar_dt)
                    if ovl_mult <= 0.0:
                        continue

                    fill_price = self._simulate_fill(bar_close, "buy", ticker)
                    atr_stop, atr_target, atr_trail, atr_activation = self._atr_adjusted_stops(
                        fill_price, df_slice, strat.strategy_id,
                    )

                    # Honour "let winners run" signal (decision.target_price=None)
                    if decision.target_price is None:
                        effective_target: float | None = None
                        effective_activation_pct = max(
                            (atr_target - fill_price) / fill_price, 0.0
                        ) if fill_price > 0 else atr_activation
                    else:
                        effective_target = atr_target
                        effective_activation_pct = atr_activation

                    equity = st.cash_usd + sum(
                        t.entry_price * t.shares for t in st.open_positions
                    )
                    vt = self._compute_vol_multiplier(st)
                    shares = self._size_by_risk(
                        fill_price, atr_stop, equity,
                        risk_per_trade_pct=0.02,
                        max_position_pct=0.40,
                        fractional=True,
                        vol_multiplier=vt.multiplier,
                    )
                    if ovl_mult != 1.0:
                        shares = round(shares * ovl_mult, 4)
                    if shares <= 0.001:
                        continue
                    cost = fill_price * shares
                    if cost > st.cash_usd:
                        shares = round(st.cash_usd / fill_price, 4)
                        if shares <= 0.001:
                            continue
                        cost = fill_price * shares

                    trade = StrategyTrade(
                        strategy_id=strat.strategy_id,
                        ticker=ticker,
                        exchange="NYSE",
                        entry_time=bar_dt,
                        entry_price=fill_price,
                        shares=shares,
                        stop_price=atr_stop,
                        target_price=effective_target,
                        trail_pct=atr_trail,
                        signals={**decision.signals, "atr_stop": atr_stop},
                        hold_type=decision.hold_type.value,
                        sentiment_score=decision.sentiment_score,
                        trail_activation_pct=effective_activation_pct,
                        highest_price=fill_price,
                    )
                    st.open_positions.append(trade)
                    st.cash_usd -= cost
                    st.trade_count += 1

            # End of day: mark-to-market
            for sid, st in states.items():
                pos_value = 0.0
                for trade in st.open_positions:
                    tdf = day_bars.get(trade.ticker)
                    if tdf is not None and not tdf.empty:
                        pos_value += float(tdf.iloc[-1]["close"]) * trade.shares
                    else:
                        pos_value += trade.entry_price * trade.shares
                total_now = st.cash_usd + pos_value
                if total_now > st.peak_equity_usd:
                    st.peak_equity_usd = total_now
                dd_pct = (st.peak_equity_usd - total_now) / st.peak_equity_usd * 100
                if dd_pct > st.max_drawdown_pct:
                    st.max_drawdown_pct = dd_pct
                total_before = day_start_equity[sid]
                if total_before > 0:
                    st.daily_returns.append(
                        (total_now - total_before) / total_before * 100
                    )
                for trade in st.open_positions:
                    trade.days_held += 1

            if (day_idx + 1) % 20 == 0:
                carried = sum(len(st.open_positions) for st in states.values())
                logger.info(
                    "Day %d/%d (%s) — %d open positions",
                    day_idx + 1, len(trading_days), day.isoformat(), carried,
                )

        # Force-close remaining
        for strat in self._strategies:
            st = states[strat.strategy_id]
            for trade in list(st.open_positions):
                end_time = datetime(
                    to_date.year, to_date.month, to_date.day,
                    16, 0, 0, tzinfo=TZ_EASTERN,
                )
                self._close_trade(st, trade, trade.entry_price, "backtest_end", end_time)

        if regime_filter:
            logger.info("Regime filter blocked %d entry evaluations", entries_blocked)

        strategy_results: list[StrategyResult] = []
        for strat in self._strategies:
            st = states[strat.strategy_id]
            sr = self._build_strategy_result(strat, st)
            strategy_results.append(sr)

        return MultiStrategyResult(
            from_date=from_date.isoformat(),
            to_date=to_date.isoformat(),
            trading_days=len(trading_days),
            strategies=strategy_results,
        )


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_comparison_report(result: MultiStrategyResult) -> str:
    """Format a comparison table of all strategies."""
    lines: list[str] = []
    W = 95

    lines.append("=" * W)
    lines.append("MULTI-STRATEGY BACKTEST COMPARISON")
    lines.append(f"Period: {result.from_date} to {result.to_date} ({result.trading_days} trading days)")
    lines.append("=" * W)

    # Header
    lines.append(
        f"{'Strategy':<20} {'Trades':>6} {'Wins':>5} {'Win%':>6} "
        f"{'P&L ($)':>10} {'Return':>8} {'MaxDD':>7} {'PF':>6} {'Sharpe':>7} "
        f"{'AvgHold':>8}"
    )
    lines.append("-" * W)

    best_return: float = -999
    best_id: str = ""

    for sr in result.strategies:
        pf_str = f"{sr.profit_factor:.2f}" if sr.profit_factor is not None else "N/A"
        pnl_sign = "+" if sr.total_pnl_usd >= 0 else ""

        lines.append(
            f"{sr.display_name:<20} {sr.total_trades:>6} {sr.wins:>5} "
            f"{sr.win_rate:>5.1f}% "
            f"{pnl_sign}${sr.total_pnl_usd:>8.2f} "
            f"{sr.return_pct:>+7.2f}% "
            f"{-sr.max_drawdown_pct:>6.1f}% "
            f"{pf_str:>6} "
            f"{sr.sharpe_approx:>6.2f} "
            f"{sr.avg_hold_minutes:>7.0f}m"
        )

        if sr.return_pct > best_return:
            best_return = sr.return_pct
            best_id = sr.display_name

    lines.append("-" * W)

    # Per-strategy equity curves (abbreviated)
    for sr in result.strategies:
        lines.append(
            f"\n{sr.display_name}: ${sr.initial_cash_usd:.0f} -> ${sr.final_cash_usd:.2f} "
            f"({sr.return_pct:+.2f}%)"
        )
        # Top trades by P&L
        if sr.trades:
            sorted_trades = sorted(sr.trades, key=lambda t: t.net_pnl_usd, reverse=True)
            lines.append("  Best trades:")
            for t in sorted_trades[:3]:
                exit_px = f"${t.exit_price:.2f}" if t.exit_price else "N/A"
                lines.append(
                    f"    {t.ticker} {t.entry_time.strftime('%Y-%m-%d')} "
                    f"${t.entry_price:.2f}->{exit_px} "
                    f"P&L=${t.net_pnl_usd:+.2f} ({t.exit_reason})"
                )
            lines.append("  Worst trades:")
            for t in sorted_trades[-3:]:
                exit_px = f"${t.exit_price:.2f}" if t.exit_price else "N/A"
                lines.append(
                    f"    {t.ticker} {t.entry_time.strftime('%Y-%m-%d')} "
                    f"${t.entry_price:.2f}->{exit_px} "
                    f"P&L=${t.net_pnl_usd:+.2f} ({t.exit_reason})"
                )

    lines.append("")
    lines.append("=" * W)
    lines.append(f">>> WINNER: {best_id} ({best_return:+.2f}% return)")
    lines.append("=" * W)

    return "\n".join(lines)


def save_comparison_json(
    result: MultiStrategyResult,
    output_path: str | None = None,
) -> str:
    """Save comparison results to JSON."""
    import os

    payload: dict[str, Any] = {
        "from_date": result.from_date,
        "to_date": result.to_date,
        "trading_days": result.trading_days,
        "strategies": [],
    }

    for sr in result.strategies:
        strat_data: dict[str, Any] = {
            "strategy_id": sr.strategy_id,
            "display_name": sr.display_name,
            "initial_cash_usd": sr.initial_cash_usd,
            "final_cash_usd": round(sr.final_cash_usd, 2),
            "total_trades": sr.total_trades,
            "wins": sr.wins,
            "losses": sr.losses,
            "total_pnl_usd": round(sr.total_pnl_usd, 2),
            "return_pct": round(sr.return_pct, 2),
            "max_drawdown_pct": round(sr.max_drawdown_pct, 2),
            "win_rate": round(sr.win_rate, 1),
            "profit_factor": round(sr.profit_factor, 2) if sr.profit_factor else None,
            "sharpe_approx": round(sr.sharpe_approx, 2),
            "avg_hold_minutes": round(sr.avg_hold_minutes, 1),
            "trades": [
                {
                    "ticker": t.ticker,
                    "entry_time": t.entry_time.isoformat(),
                    "entry_price": round(t.entry_price, 4),
                    "shares": t.shares,
                    "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                    "exit_price": round(t.exit_price, 4) if t.exit_price else None,
                    "exit_reason": t.exit_reason,
                    "pnl_usd": round(t.net_pnl_usd, 2),
                }
                for t in sr.trades
            ],
        }
        payload["strategies"].append(strat_data)

    if output_path is None:
        out_dir = os.path.join(os.getcwd(), "backtest_results")
        os.makedirs(out_dir, exist_ok=True)
        now = datetime.now(ZoneInfo("Europe/London"))
        ts = now.strftime("%Y%m%dT%H%M%S")
        output_path = os.path.join(
            out_dir,
            f"multi_strategy_{result.from_date}_to_{result.to_date}_{ts}.json",
        )

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    logger.info("Results saved to %s", output_path)
    return output_path


def save_walkforward_json(
    result: "WalkforwardResult",
    output_path: str | None = None,
) -> str:
    """Save a walkforward run (per-window stats, aggregate, bootstrap CIs)."""
    import os

    payload: dict[str, Any] = {
        "from_date": result.from_date,
        "to_date": result.to_date,
        "config": {
            "window_days": result.config.window_days,
            "step_days": result.config.step_days,
            "bootstrap_samples": result.config.bootstrap_samples,
            "bootstrap_ci": result.config.bootstrap_ci,
            "min_trades_per_window": result.config.min_trades_per_window,
        },
        "windows": [
            {
                "window_idx": w.window_idx,
                "from_date": w.from_date.isoformat(),
                "to_date": w.to_date.isoformat(),
            }
            for w in result.windows
        ],
        "aggregate": result.aggregate,
        "per_window": {
            sid: [
                {
                    "window_idx": s.window_idx,
                    "from_date": s.from_date,
                    "to_date": s.to_date,
                    "trades": s.trades,
                    "return_pct": round(s.return_pct, 4),
                    "win_rate": round(s.win_rate, 4),
                    "profit_factor": (
                        round(s.profit_factor, 4) if s.profit_factor is not None else None
                    ),
                    "max_drawdown_pct": round(s.max_drawdown_pct, 4),
                }
                for s in stats
            ]
            for sid, stats in result.per_window.items()
        },
        "bootstrap": {
            sid: {name: ci.as_dict() for name, ci in cis.items()}
            for sid, cis in result.bootstrap.items()
        },
    }

    if output_path is None:
        out_dir = os.path.join(os.getcwd(), "backtest_results")
        os.makedirs(out_dir, exist_ok=True)
        now = datetime.now(ZoneInfo("Europe/London"))
        ts = now.strftime("%Y%m%dT%H%M%S")
        output_path = os.path.join(
            out_dir,
            f"walkforward_{result.from_date}_to_{result.to_date}_{ts}.json",
        )

    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)

    logger.info("Walkforward results saved to %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main() -> None:
    """CLI entry point for multi-strategy backtest."""
    from trading_bot.log_setup import setup_logging

    log_path = setup_logging("multi_strategy_backtest")

    parser = argparse.ArgumentParser(
        description="Run multi-strategy backtest over historical data"
    )
    parser.add_argument("--from", dest="from_date", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument(
        "--cash", type=float, default=1000.0,
        help="Cash per strategy in USD (default: 1000.0)",
    )
    parser.add_argument("--download", action="store_true", help="Download data before backtesting")
    parser.add_argument(
        "--daily", action="store_true",
        help="Use S&P 500 daily CSV data instead of Alpaca intraday bars",
    )
    parser.add_argument("--min-volume", type=int, default=1_000_000, help="Min avg daily volume filter (daily mode)")
    parser.add_argument("--min-price", type=float, default=5.0, help="Min avg price filter (daily mode)")
    parser.add_argument("--max-price", type=float, default=500.0, help="Max avg price filter (daily mode)")
    parser.add_argument(
        "--strategies", type=str, default=None,
        help="Comma-separated strategy IDs to run (e.g., mean_reversion,sentiment_combo)",
    )
    parser.add_argument(
        "--no-regime-filter", action="store_true",
        help="Disable market regime filter (daily mode)",
    )
    parser.add_argument(
        "--no-calendar-overlay", action="store_true",
        help="Bypass calendar-effect overlay (clean A/B baseline). Live + "
             "backtest read calendar_overlay.enabled from config; this flag "
             "forces it off in backtest regardless.",
    )
    parser.add_argument(
        "--spy", action="store_true",
        help="Use SPY 1-min intraday CSV data (resampled to 5-min)",
    )
    parser.add_argument(
        "--multi-intraday", action="store_true",
        help="Use Alpaca 1-min cache for multiple tickers (resampled to 5-min)",
    )
    parser.add_argument(
        "--tickers", type=str, default="SPY,QQQ,XLF,XLK",
        help="Comma-separated tickers for --multi-intraday mode",
    )
    parser.add_argument(
        "--walkforward", action="store_true",
        help="Run rolling-window OOS evaluation with bootstrap CIs",
    )
    parser.add_argument(
        "--wf-window", type=int, default=90,
        help="Walkforward window size in calendar days (default: 90)",
    )
    parser.add_argument(
        "--wf-step", type=int, default=90,
        help="Walkforward step size in calendar days (default: 90)",
    )
    parser.add_argument(
        "--wf-bootstrap", type=int, default=1000,
        help="Bootstrap resamples for walkforward CIs (default: 1000)",
    )
    parser.add_argument(
        "--wf-ci", type=float, default=0.95,
        help="Walkforward CI coverage in (0, 1) (default: 0.95)",
    )
    args = parser.parse_args()

    logger.info(
        "Args: from=%s to=%s config=%s cash=%.0f download=%s daily=%s strategies=%s regime=%s",
        args.from_date, args.to_date, args.config, args.cash, args.download, args.daily,
        args.strategies, not args.no_regime_filter,
    )

    config = Config.load(args.config)
    d_from = date.fromisoformat(args.from_date)
    d_to = date.fromisoformat(args.to_date)

    if args.download:
        from dotenv import load_dotenv
        load_dotenv()
        from trading_bot.data.alpaca_downloader import download_all
        raw = config._raw.get("watchlist", {})
        tickers = list(raw.get("us", []))
        await download_all(config, tickers, d_from, d_to)

    # Validate the strategy filter early so we fail fast on a typo'd
    # strategy id, rather than running an empty backtest per window.
    if args.strategies:
        selected = set(s.strip() for s in args.strategies.split(","))
        probe = MultiStrategyBacktester(config)
        matched = [s.strategy_id for s in probe._strategies if s.strategy_id in selected]
        logger.info(
            "Strategy filter: %d -> %d strategies (%s)",
            len(probe._strategies), len(matched), matched,
        )
        if not matched:
            logger.error("No matching strategies found for: %s", selected)
            return

    ticker_list = [t.strip() for t in args.tickers.split(",") if t.strip()]

    async def _run_window(d1: date, d2: date) -> MultiStrategyResult:
        # Each window needs a fresh engine so per-strategy state
        # (cash, peak equity, cooldowns) doesn't leak across windows.
        window_engine = MultiStrategyBacktester(
            config,
            calendar_overlay_enabled=not args.no_calendar_overlay,
        )
        if args.strategies:
            selected = set(s.strip() for s in args.strategies.split(","))
            window_engine._strategies = [
                s for s in window_engine._strategies if s.strategy_id in selected
            ]
        if args.multi_intraday:
            return await window_engine.run_multi_ticker_intraday(
                d1, d2,
                tickers=ticker_list,
                cash_per_strategy_usd=args.cash,
                regime_filter=not args.no_regime_filter,
            )
        if args.spy:
            return await window_engine.run_spy_intraday(
                d1, d2,
                cash_per_strategy_usd=args.cash,
                regime_filter=not args.no_regime_filter,
            )
        if args.daily:
            return await window_engine.run_daily(
                d1, d2,
                cash_per_strategy_usd=args.cash,
                min_avg_volume=args.min_volume,
                min_price=args.min_price,
                max_price=args.max_price,
                regime_filter=not args.no_regime_filter,
            )
        return await window_engine.run(d1, d2, cash_per_strategy_usd=args.cash)

    if args.walkforward:
        from trading_bot.backtest.walkforward import (
            WalkforwardConfig,
            run_walkforward,
        )

        wf_cfg = WalkforwardConfig(
            window_days=args.wf_window,
            step_days=args.wf_step,
            bootstrap_samples=args.wf_bootstrap,
            bootstrap_ci=args.wf_ci,
        )
        logger.info(
            "Running walkforward: window=%dd step=%dd bootstrap=%d ci=%.2f",
            wf_cfg.window_days, wf_cfg.step_days,
            wf_cfg.bootstrap_samples, wf_cfg.bootstrap_ci,
        )
        wf_result = await run_walkforward(d_from, d_to, _run_window, config=wf_cfg)
        report = wf_result.summary()
        for line in report.splitlines():
            logger.info(line)
        print(report)

        json_path = save_walkforward_json(wf_result)
        logger.info("Walkforward JSON saved to %s", json_path)
        logger.info("Log file: %s", log_path)
        print(f"\nWalkforward results saved to: {json_path}")
        print(f"Log file: {log_path}")
        return

    if args.multi_intraday:
        logger.info("Running multi-ticker intraday backtest on %s", ticker_list)
        result = await _run_window(d_from, d_to)
    elif args.spy:
        logger.info("Running SPY intraday backtest (5-min bars)")
        result = await _run_window(d_from, d_to)
    elif args.daily:
        logger.info("Running daily-bar backtest on S&P 500 universe")
        result = await _run_window(d_from, d_to)
    else:
        result = await _run_window(d_from, d_to)

    report = format_comparison_report(result)
    for line in report.splitlines():
        logger.info(line)
    print(report)

    json_path = save_comparison_json(result)
    logger.info("JSON results saved to %s", json_path)
    logger.info("Log file: %s", log_path)
    print(f"\nResults saved to: {json_path}")
    print(f"\nLog file: {log_path}")


if __name__ == "__main__":
    asyncio.run(main())

"""Mean Reversion strategy — buy RSI oversold bounces."""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.constants import HoldType, TZ_EASTERN
from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.technical import TechnicalAnalyzer
from trading_bot.utils import coalesce
from trading_bot.utils.time import count_trading_days_between

logger: logging.Logger = logging.getLogger(__name__)

# Trading days per year — annualisation factor for realized volatility.
_TRADING_DAYS_PER_YEAR: int = 252


class MeanReversionStrategy(StrategyBase):
    """Buy when RSI(14) recovers from oversold; exit on RSI normalization or target."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            strategy_id="mean_reversion",
            display_name="Mean Reversion",
            config=config,
            **kwargs,
        )
        self._rsi_period: int = int(config.get("rsi_period", 14))
        self._rsi_oversold: float = float(config.get("rsi_oversold", 28))
        self._rsi_recovery: float = float(config.get("rsi_recovery", 35))
        self._rsi_exit: float = float(config.get("rsi_exit", 55))
        self._stop_loss_pct: float = float(config.get("stop_loss_pct", 0.02))
        self._take_profit_pct: float = float(config.get("take_profit_pct", 0.03))
        self._max_positions: int = int(config.get("max_positions", 1))
        self._volume_multiplier: float = float(config.get("volume_multiplier", 1.3))
        self._oversold_lookback: int = int(config.get("oversold_lookback", 5))
        self._ema_confirm_period: int = int(config.get("ema_confirm_period", 9))
        self._require_ema_confirm: bool = bool(config.get("require_ema_confirm", False))
        # ATR-based stop/target/trail + risk sizing (match backtester)
        self._use_atr_stops: bool = bool(config.get("use_atr_stops", False))
        self._use_risk_sizing: bool = bool(config.get("use_risk_sizing", False))
        self._atr_period: int = int(config.get("atr_period", 14))
        self._atr_stop_mult: float = float(config.get("atr_stop_mult", 2.0))
        self._atr_target_mult: float = float(config.get("atr_target_mult", 5.0))
        self._atr_trail_mult: float = float(config.get("atr_trail_mult", 2.5))
        self._atr_activation_mult: float = float(config.get("atr_activation_mult", 2.5))
        self._risk_per_trade_pct: float = float(config.get("risk_per_trade_pct", 0.02))
        self._max_position_pct: float = float(config.get("max_position_pct", 0.40))
        # Default True for consistency with overnight_drift / ORB and to
        # match the ai-broker#39 plain-limit + standalone-stop entry path,
        # which is the path the $1k live account *must* take. Whole shares
        # at $1k with SPY/QQQ/XLK >$400 floors to 0, dropping ~50% of
        # mean_reversion signals — see PR #38 / issue ai-broker#39. Live
        # config.yaml sets this explicitly to True; the default exists so
        # a forgotten override does not silently regress to the floor.
        self._fractional_shares: bool = bool(config.get("fractional_shares", True))
        # Let winners run past the target. When true, the ATR-derived target
        # is used as the trailing-stop activation price (not a hard exit).
        # Exits become: stop loss, trailing stop after activation, or RSI exit.
        self._let_winners_run: bool = bool(config.get("let_winners_run", False))
        # Once a winning position has moved at least this % above entry, the
        # RSI-normalisation exit is disabled and the trailing stop (set up at
        # entry) takes over. Keeps the runners running.
        self._let_winners_run_up_pct: float = float(
            config.get("let_winners_run_up_pct", 0.03)
        )
        # Volatility-adaptive RSI thresholds: tighten in high-vol regimes
        # (deeper dip = higher-quality signal), relax in low-vol (more trades).
        # Realized vol is annualised stddev of daily log returns, in %.
        self._vix_adaptive: bool = bool(config.get("vix_adaptive_rsi", False))
        self._rv_high_threshold: float = float(config.get("rv_high_threshold", 25.0))
        self._rv_low_threshold: float = float(config.get("rv_low_threshold", 12.0))
        self._rsi_oversold_high_vol: float = float(config.get("rsi_oversold_high_vol", 25))
        self._rsi_oversold_low_vol: float = float(config.get("rsi_oversold_low_vol", 30))
        self._rv_lookback_days: int = int(config.get("rv_lookback_days", 20))
        # Bollinger Band confirmation (#4 from return_improvement_todos).
        # Require that price touched or penetrated the lower Bollinger Band
        # within a recent lookback AND has closed back above the band. Acts
        # as a second independent mean-reversion signal that complements RSI.
        # Opt-in — disabled by default so the validated RSI-only path is
        # unchanged until a backtest confirms lift.
        self._require_bb_confirm: bool = bool(config.get("require_bb_confirm", False))
        self._bb_period: int = int(config.get("bb_period", 20))
        self._bb_std: float = float(config.get("bb_std", 2.0))
        self._bb_lookback_bars: int = int(config.get("bb_lookback_bars", 3))
        # Time stop: maximum trading-day hold before force-exit.  Prevents
        # breakeven positions from sitting indefinitely when neither the stop,
        # target, nor RSI normalisation triggers.  Default 5 trading days
        # matches ExitManager.check_time_stop's swing default.
        self._max_hold_days: int = int(config.get("max_hold_days", 5))

    @staticmethod
    def _realized_vol_pct(df_daily: pd.DataFrame, lookback_days: int = 20) -> float | None:
        """Annualised realized volatility (%) from daily closes.

        Uses log returns over the last ``lookback_days``, × √252 × 100.
        Returns ``None`` if insufficient data. Serves as a cheap VIX proxy —
        VIX measures implied vol but tracks realized vol closely on average
        (within ~3-5 vol points).
        """
        if df_daily is None or len(df_daily) < lookback_days + 1:
            return None
        closes = df_daily["close"].astype(float).dropna()
        if len(closes) < lookback_days + 1:
            return None
        log_returns = np.log(closes / closes.shift(1)).dropna().iloc[-lookback_days:]
        if len(log_returns) < lookback_days // 2:
            return None
        vol = float(log_returns.std() * (_TRADING_DAYS_PER_YEAR ** 0.5) * 100)
        return vol if vol > 0 else None

    def _adaptive_rsi_oversold(self, df_daily: pd.DataFrame) -> tuple[float, str]:
        """Return the RSI-oversold threshold to use given current volatility.

        Returns (threshold, regime_label). When ``vix_adaptive_rsi`` is off,
        returns the static configured value.
        """
        if not self._vix_adaptive:
            return self._rsi_oversold, "static"

        rv = self._realized_vol_pct(df_daily, self._rv_lookback_days)
        if rv is None:
            return self._rsi_oversold, "static (rv-unavailable)"

        if rv >= self._rv_high_threshold:
            return self._rsi_oversold_high_vol, f"high-vol (rv={rv:.1f}%)"
        if rv <= self._rv_low_threshold:
            return self._rsi_oversold_low_vol, f"low-vol (rv={rv:.1f}%)"
        return self._rsi_oversold, f"normal-vol (rv={rv:.1f}%)"

    def _check_bb_bounce(self, df_5min: pd.DataFrame) -> bool:
        """True when a recent lower-band touch has been followed by a close
        back above the lower band.

        Computes Bollinger Bands inline to avoid depending on precomputed
        indicators, since the backtester does not enrich the df with BB
        columns. Lookback window ``bb_lookback_bars`` mirrors the TODO
        description ("within 3 bars of RSI oversold recovery").
        """
        if len(df_5min) < self._bb_period + self._bb_lookback_bars:
            return False

        df = df_5min.rename(columns=str.lower)
        for col in ("close", "low"):
            if col not in df.columns:
                return False

        close = df["close"].astype(float)
        low = df["low"].astype(float)
        bb_mid = close.rolling(self._bb_period).mean()
        bb_std = close.rolling(self._bb_period).std(ddof=0)
        bb_lower = bb_mid - self._bb_std * bb_std

        if bb_lower.isna().iloc[-1] or math.isnan(float(close.iloc[-1])):
            return False

        # Current close must be above the current lower band.
        if float(close.iloc[-1]) <= float(bb_lower.iloc[-1]):
            return False

        # In the recent lookback (excluding the current bar), some bar's low
        # must have touched/penetrated the lower band.
        tail_low = low.iloc[-(self._bb_lookback_bars + 1):-1]
        tail_band = bb_lower.iloc[-(self._bb_lookback_bars + 1):-1]
        touched = (tail_low <= tail_band).any()
        return bool(touched)

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
        if len(df_5min) < max(self._rsi_period + 5, 21):
            return None

        rsi: pd.Series = TechnicalAnalyzer.compute_rsi(df_5min, self._rsi_period)
        if rsi.isna().all():
            return None

        current_rsi: float = float(rsi.iloc[-1])
        if math.isnan(current_rsi):
            return None

        # Volatility-adaptive oversold threshold
        rsi_oversold, vol_regime = self._adaptive_rsi_oversold(df_daily)

        # Tighter check: RSI was below oversold in a short lookback and has
        # crossed back above a higher recovery threshold (reduces false signals)
        lookback: pd.Series = rsi.iloc[-self._oversold_lookback:]
        was_oversold: bool = bool((lookback < rsi_oversold).any())
        has_recovered: bool = current_rsi > self._rsi_recovery

        if not (was_oversold and has_recovered):
            return None

        # Volume confirmation: current bar volume must exceed average
        df_e: pd.DataFrame = df_5min.rename(columns=str.lower)
        vol_avg: float = float(df_e["volume"].rolling(20).mean().iloc[-1])
        current_vol: float = float(df_e["volume"].iloc[-1])
        if vol_avg <= 0 or current_vol < self._volume_multiplier * vol_avg:
            return None

        # EMA confirmation: price must be back above short-term EMA
        # (filters out "falling knife" entries where RSI recovered briefly
        # but price is still in a strong downtrend)
        if self._require_ema_confirm and len(df_e) >= self._ema_confirm_period:
            ema = df_e["close"].ewm(span=self._ema_confirm_period, adjust=False).mean()
            current_ema = float(ema.iloc[-1])
            if current_price < current_ema:
                return None

        # Bollinger Band confirmation — second independent mean-reversion
        # signal. Price must have touched the lower band recently and
        # reverted above it.
        bb_confirmed: bool = False
        if self._require_bb_confirm:
            bb_confirmed = self._check_bb_bounce(df_5min)
            if not bb_confirmed:
                return None

        trail_pct: float | None = None
        trail_activation_price: float | None = None
        target_price_computed: float | None = None
        if self._use_atr_stops:
            stop_price, target_price_computed, trail_pct, trail_activation_price = (
                self.atr_adjusted_stops(
                    entry_price=current_price,
                    df=df_5min,
                    atr_period=self._atr_period,
                    stop_atr_mult=self._atr_stop_mult,
                    target_atr_mult=self._atr_target_mult,
                    trail_atr_mult=self._atr_trail_mult,
                    activation_atr_mult=self._atr_activation_mult,
                    fallback_stop_pct=self._stop_loss_pct,
                    fallback_target_pct=self._take_profit_pct,
                )
            )
        else:
            stop_price = round(current_price * (1.0 - self._stop_loss_pct), 2)
            target_price_computed = round(current_price * (1.0 + self._take_profit_pct), 2)

        # Let winners run: promote the target to the trailing-stop activation
        # price, and disable the hard target exit. Exits then come from stop,
        # trailing stop, or RSI normalisation only.
        if self._let_winners_run and target_price_computed is not None:
            trail_activation_price = target_price_computed
            target_price: float | None = None
        else:
            target_price = target_price_computed

        if self._use_risk_sizing:
            vt_mult: float = self.vol_multiplier()
            shares = self.size_by_risk(
                entry_price=current_price,
                stop_price=stop_price,
                available_cash=available_cash,
                risk_per_trade_pct=self._risk_per_trade_pct,
                max_position_pct=self._max_position_pct,
                fractional=self._fractional_shares,
                vol_multiplier=vt_mult,
            )
        else:
            shares = self._compute_shares(current_price, stop_price, available_cash)

        # Minimum viable size: 1 whole share OR a meaningful fractional (>=0.001)
        min_shares: float = 0.001 if self._fractional_shares else 1.0
        if shares < min_shares:
            return None

        logger.info(
            "[%s] Mean reversion entry signal: %s RSI=%.1f [%s, oversold<%g], %.4f shares @ $%.2f",
            self.strategy_id, ticker, current_rsi, vol_regime, rsi_oversold,
            shares, current_price,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=shares,
            entry_price=current_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_pct=trail_pct,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={
                "rsi": current_rsi,
                "was_oversold": True,
                "oversold_threshold": rsi_oversold,
                "vol_regime": vol_regime,
                "bb_confirmed": bb_confirmed,
            },
            sentiment_score=sentiment_score,
            trail_activation_price=trail_activation_price,
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

        # Take profit — prefer the explicitly-stored target (e.g. ATR-derived)
        stored_target: float = float(coalesce(position, "target_price", 0))
        if stored_target > 0:
            if current_price >= stored_target:
                return ExitSignal(should_exit=True, reason="take_profit")
        elif entry_price > 0:
            target: float = entry_price * (1.0 + self._take_profit_pct)
            if current_price >= target:
                return ExitSignal(should_exit=True, reason="take_profit")

        # Time stop — prevent indefinite holds at breakeven.  Counts actual
        # trading days so weekends / NYSE holidays do not consume the budget.
        # Fires before the RSI check so a stalled position is always released
        # within _max_hold_days sessions regardless of RSI state.
        entry_time_raw: Any = position.get("entry_time")
        if entry_time_raw is not None:
            try:
                entry_dt: datetime = datetime.fromisoformat(str(entry_time_raw))
                if entry_dt.tzinfo is None:
                    entry_dt = entry_dt.replace(tzinfo=TZ_EASTERN)
                now_et: datetime = datetime.now(tz=TZ_EASTERN)
                cal: HolidayCalendar = HolidayCalendar()
                elapsed_days: int = count_trading_days_between(
                    cal,
                    entry_dt.astimezone(TZ_EASTERN).date(),
                    now_et.date(),
                )
                if elapsed_days >= self._max_hold_days:
                    logger.info(
                        "[mean_reversion] time_stop: %s held %d trading days "
                        "(max=%d) — exiting",
                        position.get("ticker", "?"), elapsed_days, self._max_hold_days,
                    )
                    return ExitSignal(should_exit=True, reason="time_stop")
            except Exception:
                logger.warning(
                    "time_stop check failed for position id=%s",
                    position.get("id"), exc_info=True,
                )

        # RSI-based exit — fully disabled when let_winners_run is on, so the
        # trailing stop (set up at entry with activation threshold) becomes the
        # primary exit for winning trades. Exits then come from: stop_loss,
        # target (if not nulled), or trailing stop after activation.
        skip_rsi_exit: bool = bool(self._let_winners_run)

        if not skip_rsi_exit and df_5min is not None and len(df_5min) >= self._rsi_period + 1:
            rsi: pd.Series = TechnicalAnalyzer.compute_rsi(df_5min, self._rsi_period)
            current_rsi: float = float(rsi.iloc[-1])
            if not math.isnan(current_rsi) and current_rsi > self._rsi_exit:
                return ExitSignal(should_exit=True, reason="rsi_normalized")

        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

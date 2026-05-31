"""Opening gap-fill strategy (sleeve #9, ai-broker#48).

Fades large overnight gaps in liquid ETFs at the open and targets the prior
close. Captures the well-documented intraday gap-reversion tendency: opening
gaps in liquid ETFs fill a large fraction of the time intraday.

Mechanics (one entry per ticker per session, enforced structurally by the
first-bar-only entry gate):

Entry  : on the session's FIRST 5-min bar, if the gap from the prior
         (adjusted) daily close exceeds an adaptive threshold. Gap up → SHORT
         (fade), gap down → LONG (fade). Entry price = that bar's close.
Target : the prior adjusted close (full gap fill).
Stop   : ATR-anchored (``stop_atr_multiplier`` × overnight ATR%) with a
         percentage FLOOR (``stop_pct_floor``) so a pathologically small ATR
         can't collapse the stop and explode position size.
Exit   : target / stop (handled intrabar by the backtester and broker-side
         live), plus a hard 14:00 ET time stop — an unfilled gap by midday is
         a trending regime, so we get out rather than risk gap continuation.

Bar-timestamp convention (CRITICAL — a known off-by-one source):
The repo resamples 5-min bars LEFT-labelled (``label="left", closed="left"``),
so the session's OPEN bar is labelled 09:30 and covers 09:30:00–09:34:59. The
gap reference is that open bar's OPEN (the opening print). We *act* on the bar
labelled 09:35 — the first bar inside the 09:35 execution window (the backtester
and live config both skip the first 5 minutes) — giving the gap ~5 minutes to
settle, as issue #48 intended (the issue assumed right-labelling, where "the
09:35 bar" was the open bar). Acting on the 09:35 bar also means the entry
clears the backtester's execution-window and warm-up gates; the warm-up gate is
relaxed for this sleeve via ``min_warmup_bars`` since it keys off the open and
daily history rather than intraday indicator history.

Hold type is INTRADAY so the backtester / live wind-down force-closes any
position still open at session end.
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


class GapFillStrategy(StrategyBase):
    """Fade the opening gap; target the prior close."""

    def __init__(self, config: dict[str, Any], **kwargs: Any) -> None:
        super().__init__(
            strategy_id="gap_fill",
            display_name="Opening Gap Fill",
            config=config,
            **kwargs,
        )
        self._max_positions: int = int(config.get("max_positions", 3))
        # Label-time of the bar we act on — the first bar inside the 09:35
        # execution window. The gap is measured from the session open (the
        # 09:30 bar's open); we enter at this bar. Firing on exactly this
        # label makes the entry inherently once-per-session.
        self._entry_time: time = _parse_time(str(config.get("entry_time_et", "09:35")))
        # Hard midday time stop (ET): close an unfilled gap rather than ride a
        # trend continuation into the afternoon.
        self._time_stop: time = _parse_time(str(config.get("time_stop_et", "14:00")))
        # Adaptive gap threshold: max(floor, multiplier × overnight ATR%).
        self._min_gap_pct: float = float(config.get("min_gap_pct", 0.005))
        self._gap_atr_multiplier: float = float(config.get("gap_atr_multiplier", 0.5))
        self._overnight_atr_period: int = int(config.get("overnight_atr_period", 14))
        # Stop: ATR-anchored with a percentage floor (minimum distance).
        self._stop_atr_multiplier: float = float(config.get("stop_atr_multiplier", 1.5))
        self._stop_pct_floor: float = float(config.get("stop_pct_floor", 0.008))
        # Risk-anchored sizing.
        self._risk_per_trade_pct: float = float(config.get("risk_per_trade_pct", 0.005))
        self._max_position_pct: float = float(config.get("max_position_pct", 0.33))
        self._fractional_shares: bool = bool(config.get("fractional_shares", True))

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
        if df_5min is None or len(df_5min) == 0:
            return None
        if df_daily is None or len(df_daily) == 0:
            return None
        if available_cash <= 0 or current_price <= 0:
            return None

        df_e: pd.DataFrame = df_5min.rename(columns=str.lower)
        last_dt: datetime | None = _to_et_datetime(df_e.index[-1])
        if last_dt is None:
            return None

        # Act only on the configured entry bar (default 09:35) — the first
        # bar inside the execution window. Exactly-once per session, so no
        # explicit per-day flag is needed. A halted/late session with no
        # 09:35 bar is simply skipped.
        if last_dt.time() != self._entry_time:
            return None

        today: date = last_dt.date()
        today_bars: pd.DataFrame = df_e[
            df_e.index.map(lambda ts: _to_et_date(ts) == today)
        ]
        if len(today_bars) == 0:
            return None

        # Prior (adjusted) close: the last daily bar STRICTLY before today.
        # Using rows < today avoids any look-ahead onto today's daily bar that
        # some cache layouts include. Adjusted close handles ex-div/split days
        # cleanly — a raw last-bar close would read an ex-div day as a 1-3% gap.
        def _before_today(ts: Any) -> bool:
            d: date | None = _to_et_date(ts)
            return d is not None and d < today

        daily_before: pd.DataFrame = df_daily[df_daily.index.map(_before_today)]
        if len(daily_before) < self._overnight_atr_period + 1:
            return None  # insufficient history for ATR / prior close
        prior_close: float = float(daily_before["close"].iloc[-1])
        if prior_close <= 0:
            return None

        session_open: float = float(today_bars["open"].iloc[0])
        gap_pct: float = (session_open - prior_close) / prior_close

        overnight_atr: float | None = self._compute_atr(
            daily_before, self._overnight_atr_period,
        )
        if overnight_atr is None:
            return None
        overnight_atr_pct: float = overnight_atr / prior_close

        threshold: float = max(
            self._min_gap_pct, self._gap_atr_multiplier * overnight_atr_pct,
        )
        if abs(gap_pct) < threshold:
            return None

        # Fade the gap. Gap up → short; gap down → long.
        is_short: bool = gap_pct > 0
        direction: str = "short" if is_short else "long"
        entry_price: float = current_price  # = the first bar's close

        # Stop: ATR-anchored, with a percentage floor (minimum distance) so a
        # tiny ATR can't collapse the stop and explode the risk-based size.
        stop_distance_pct: float = max(
            self._stop_atr_multiplier * overnight_atr_pct, self._stop_pct_floor,
        )
        if is_short:
            stop_price: float = round(entry_price * (1.0 + stop_distance_pct), 4)
        else:
            stop_price = round(entry_price * (1.0 - stop_distance_pct), 4)
        target_price: float = round(prior_close, 4)

        shares: float = self.size_by_risk(
            entry_price=entry_price,
            stop_price=stop_price,
            available_cash=available_cash,
            risk_per_trade_pct=self._risk_per_trade_pct,
            max_position_pct=self._max_position_pct,
            fractional=self._fractional_shares,
            vol_multiplier=self.vol_multiplier(),
        )
        min_shares: float = 0.001 if self._fractional_shares else 1.0
        if shares < min_shares:
            return None

        logger.info(
            "[%s] Gap-fill %s: %s gap=%.3f%% (thr=%.3f%%) open=$%.2f "
            "prior_close=$%.2f entry=$%.2f stop=$%.2f tgt=$%.2f shares=%.4f",
            self.strategy_id, direction, ticker, gap_pct * 100, threshold * 100,
            session_open, prior_close, entry_price, stop_price, target_price, shares,
        )

        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction=direction,
            shares=shares,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            trail_pct=None,
            hold_type=HoldType.INTRADAY,
            strategy_id=self.strategy_id,
            signals={
                "gap_pct": gap_pct,
                "threshold_pct": threshold,
                "prior_close": prior_close,
                "session_open": session_open,
                "overnight_atr_pct": overnight_atr_pct,
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
        is_short: bool = _position_is_short(position)
        stop_price: float = float(coalesce(position, "stop_price", 0))
        target_price: float = float(coalesce(position, "target_price", 0))

        # Hard 14:00 ET time stop — close an unfilled gap before the afternoon.
        bar_time: time | None = _last_bar_time(df_5min)
        if bar_time is not None and bar_time >= self._time_stop:
            return ExitSignal(
                should_exit=True, reason="time_stop", use_market_order=True,
            )

        # Target = prior close (full gap fill).
        if target_price > 0:
            hit_target = (
                current_price <= target_price if is_short
                else current_price >= target_price
            )
            if hit_target:
                return ExitSignal(should_exit=True, reason="take_profit")

        # Protective stop (redundant with the broker stop / backtester intrabar
        # check, but belt-and-braces for the live path).
        if stop_price > 0:
            hit_stop = (
                current_price >= stop_price if is_short
                else current_price <= stop_price
            )
            if hit_stop:
                return ExitSignal(
                    should_exit=True, reason="stop_loss",
                    is_emergency=True, use_market_order=True,
                )

        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return self._max_positions

    def min_warmup_bars(self) -> int:
        # Gap-fill keys off the session open + daily ATR history, not intraday
        # indicators, and fires on the 09:35 bar (bar index 1). Relax the
        # backtester's default 10-bar intraday warm-up so that bar isn't
        # skipped. The 09:35 execution-window start still gates out the open bar.
        return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _position_is_short(position: dict[str, Any]) -> bool:
    """Direction of a position dict, tolerating both representations.

    The live positions row carries ``side`` ("BUY"/"SELL"); the backtester's
    exit-check dict may carry ``direction`` ("long"/"short"). Either marks a
    short. Defaults to long when neither is present (backtest dicts that omit
    direction — harmless because the backtester resolves target/stop intrabar
    by trade direction before delegating to this method).
    """
    if str(position.get("direction", "")).lower() == "short":
        return True
    return str(position.get("side", "")).upper() == "SELL"


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


def _last_bar_time(df_5min: pd.DataFrame | None) -> time | None:
    if df_5min is None or len(df_5min) == 0:
        return None
    dt = _to_et_datetime(df_5min.index[-1])
    return dt.time() if dt is not None else None

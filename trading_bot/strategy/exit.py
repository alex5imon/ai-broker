"""Exit logic and position monitoring.

Implements SPEC Section 7: stop loss, take profit, trailing stop, time stop,
and spread-widening protection.  Produces :class:`ExitDecision` objects that
the order manager translates into IB orders.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.config import Config
from trading_bot.constants import TZ_EASTERN, ExitReason, HoldType
from trading_bot.data.holiday_calendar import HolidayCalendar
from trading_bot.data.market_data import MarketDataManager

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ExitDecision:
    """Result of evaluating whether a position should be exited."""

    should_exit: bool
    reason: ExitReason | None
    is_emergency: bool  # True for stop_loss, kill_switch, daily_limit
    exit_price: float | None
    use_market_order: bool  # True for emergency exits


# ---------------------------------------------------------------------------
# ExitManager
# ---------------------------------------------------------------------------


class ExitManager:
    """Monitors positions and manages exit conditions.

    All exit parameters are phase-aware: intraday parameters tighten in
    Phase 2 and 3, while swing parameters remain constant across phases.
    """

    def __init__(
        self,
        config: Config,
        market_data: MarketDataManager,
    ) -> None:
        self._config: Config = config
        self._market_data: MarketDataManager = market_data

        # Cache exit-spread protection config
        self._spread_max_pct: float = float(
            config._require("exit_spread_protection", "max_spread_pct")
        )
        self._spread_max_delay_s: int = int(
            config._require("exit_spread_protection", "max_delay_seconds")
        )
        self._spread_recheck_s: int = int(
            config._require("exit_spread_protection", "recheck_interval_seconds")
        )

        # NYSE holiday calendar for swing-trade trading-day counting.
        # ``check_time_stop`` swing branch counts actual sessions between
        # entry and now, so a Thu entry doesn't hit a 5-day stop on the
        # following Tue (which is only 2 trading days away).
        # ``raw_section()`` returns a dict copy and cannot raise — let any
        # programming error (e.g. ``_raw`` reassigned to a non-dict) fail
        # the constructor rather than silently degrade to fallback dates.
        self._holiday_calendar: HolidayCalendar = HolidayCalendar(
            config.raw_section()
        )

    # ------------------------------------------------------------------
    # Parameter helpers
    # ------------------------------------------------------------------

    def get_exit_params(self, hold_type: HoldType) -> dict[str, Any]:
        """Get phase-aware exit parameters for *hold_type*.

        Delegates to ``Config.get_exit_params`` which overlays phase
        overrides for intraday exits.
        """
        return self._config.get_exit_params(hold_type)

    # ------------------------------------------------------------------
    # Individual exit checks
    # ------------------------------------------------------------------

    def check_stop_loss(self, position: dict[str, Any], current_price: float) -> bool:
        """Check if price has hit the stop-loss level.

        For longs: current_price <= stop_price.
        For shorts: current_price >= stop_price.
        """
        stop_price: float | None = position.get("stop_price")
        if stop_price is None:
            return False

        direction: str = position.get("direction", "long")

        if direction == "long":
            return current_price <= stop_price
        # Short
        return current_price >= stop_price

    def check_take_profit(
        self, position: dict[str, Any], current_price: float
    ) -> bool:
        """Check if price has hit the take-profit target.

        For longs: current_price >= target_price.
        For shorts: current_price <= target_price.
        """
        target_price: float | None = position.get("target_price")
        if target_price is None:
            return False

        direction: str = position.get("direction", "long")

        if direction == "long":
            return current_price >= target_price
        # Short
        return current_price <= target_price

    def check_time_stop(
        self, position: dict[str, Any], current_time: datetime
    ) -> bool:
        """Check if position has exceeded maximum hold time.

        Intraday: ``time_stop_hours`` (default 4) wall-clock hours.
        Swing: ``max_hold_days`` (default 5) **trading** days, counted
        via ``HolidayCalendar`` so weekends and NYSE holidays don't
        consume the budget. A Thursday entry with ``max_hold_days=5``
        now expires the following Thursday session, not the following
        Tuesday — the calendar-day version was eating ~40% of the
        budget on weekends/holidays.
        """
        entry_time_raw: Any = position.get("entry_time")
        if entry_time_raw is None:
            return False

        # Parse entry time
        if isinstance(entry_time_raw, str):
            entry_time: datetime = datetime.fromisoformat(entry_time_raw)
        elif isinstance(entry_time_raw, datetime):
            entry_time = entry_time_raw
        else:
            return False

        # Ensure timezone-aware. The bot's writers always stamp ET via
        # ``datetime.now(tz=ET).isoformat()`` so naive strings are
        # exceptional — fixtures, manual DB inserts, or legacy rows.
        # Stamp them as ET (the writer's convention) and warn loudly so
        # the source can be fixed; treating a UTC-naive string as ET
        # would skew the time-stop by 4–5 hours.
        if entry_time.tzinfo is None:
            logger.warning(
                "Position has naive entry_time (%s) — stamping as ET. "
                "Investigate the writer; this row may have legacy data.",
                entry_time_raw,
            )
            entry_time = entry_time.replace(tzinfo=ET)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=ET)

        hold_type_str: str = position.get("hold_type", "intraday")
        hold_type: HoldType = HoldType(hold_type_str)
        params: dict[str, Any] = self.get_exit_params(hold_type)

        if hold_type == HoldType.INTRADAY:
            max_hours: float = float(params.get("time_stop_hours", 4))
            elapsed: timedelta = current_time - entry_time
            return elapsed >= timedelta(hours=max_hours)

        # Swing: count trading days between entry date and now.
        max_days: int = int(params.get("max_hold_days", 5))
        # Convert both timestamps to ET dates so the comparison is
        # session-aligned rather than UTC-anchored.
        entry_date: date = entry_time.astimezone(ET).date()
        current_date: date = current_time.astimezone(ET).date()
        elapsed_trading_days: int = self._count_trading_days_between(
            entry_date, current_date,
        )
        return elapsed_trading_days >= max_days

    def _count_trading_days_between(
        self,
        start_date: date,
        end_date: date,
    ) -> int:
        """Count NYSE trading days strictly between *start_date* (entry)
        and *end_date* (now), exclusive of the entry date and inclusive
        of any session ending on or before *end_date*.

        Examples (no holidays, no weekends shown):
        - entry Mon, now Mon → 0 (same session, intraday — the swing
          branch shouldn't fire).
        - entry Mon, now Tue → 1.
        - entry Thu, now Mon → 2 (Fri + Mon).
        - entry Thu, now Tue → 3 (Fri + Mon + Tue).

        Returns 0 when ``end_date <= start_date``. The walk is bounded
        by calendar-day distance so a corrupted timestamp can't loop
        forever.
        """
        if end_date <= start_date:
            return 0
        cal: HolidayCalendar = self._holiday_calendar
        count: int = 0
        cursor: date = start_date + timedelta(days=1)
        while cursor <= end_date:
            if cal.is_trading_day(cursor):
                count += 1
            cursor += timedelta(days=1)
        return count

    # ------------------------------------------------------------------
    # Composite exit evaluation
    # ------------------------------------------------------------------

    def should_exit(
        self,
        position: dict[str, Any],
        current_price: float,
        current_time: datetime,
    ) -> ExitDecision:
        """Evaluate all exit conditions for a position.

        Checks in priority order:
        1. Stop loss (emergency)
        2. Take profit
        3. Time stop

        Trailing stops are managed by Alpaca natively via
        ``TrailingStopOrderRequest`` (submitted by
        ``OrderManager.activate_trailing_stop`` once
        ``trail_activation_price`` is reached). The broker tracks the
        high-water mark and fires the stop autonomously, so there is no
        software-side trail check here. The previous duplicate check
        risked racing the broker's stop and never persisted
        ``highest_price`` between ticks.

        Returns an :class:`ExitDecision`.
        """
        ticker: str = position.get("ticker", "?")

        # Priority 1: Stop loss
        if self.check_stop_loss(position, current_price):
            logger.warning(
                "%s: STOP LOSS triggered at %.4f", ticker, current_price
            )
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.STOP_LOSS,
                is_emergency=True,
                exit_price=current_price,
                use_market_order=True,
            )

        # Priority 2: Take profit
        if self.check_take_profit(position, current_price):
            logger.info(
                "%s: TAKE PROFIT triggered at %.4f", ticker, current_price
            )
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TAKE_PROFIT,
                is_emergency=False,
                exit_price=current_price,
                use_market_order=False,
            )

        # Priority 3: Time stop
        if self.check_time_stop(position, current_time):
            hold_type_str: str = position.get("hold_type", "intraday")
            hold_type: HoldType = HoldType(hold_type_str)
            entry_price: float = position.get("entry_price", 0.0)

            # Time stop behaviour depends on current P&L
            if entry_price > 0:
                pnl_pct: float = (current_price - entry_price) / entry_price
                direction: str = position.get("direction", "long")
                if direction == "short":
                    pnl_pct = -pnl_pct

                use_market: bool = self._time_stop_should_use_market(
                    hold_type, pnl_pct
                )
            else:
                pnl_pct = 0.0
                use_market = True

            logger.info(
                "%s: TIME STOP triggered (P&L=%.2f%%)", ticker, pnl_pct * 100
            )
            return ExitDecision(
                should_exit=True,
                reason=ExitReason.TIME_STOP,
                is_emergency=False,
                exit_price=current_price,
                use_market_order=use_market,
            )

        # No exit
        return ExitDecision(
            should_exit=False,
            reason=None,
            is_emergency=False,
            exit_price=None,
            use_market_order=False,
        )

    # ------------------------------------------------------------------
    # Spread protection
    # ------------------------------------------------------------------

    def check_spread_for_exit(self, ticker: str) -> bool:
        """Check if the spread is acceptable for a non-emergency exit.

        Returns ``True`` if the spread is narrow enough to proceed.
        Returns ``False`` if the spread exceeds 0.15% (caller should
        delay up to 2 minutes and recheck).
        """
        spread: float | None = self._market_data.get_spread_pct(ticker)
        if spread is None:
            # No data -- proceed cautiously
            logger.warning(
                "%s: No spread data for exit check; proceeding", ticker
            )
            return True

        if spread > self._spread_max_pct:
            logger.info(
                "%s: Spread %.4f%% exceeds exit threshold %.4f%%; "
                "delaying non-emergency exit",
                ticker,
                spread * 100,
                self._spread_max_pct * 100,
            )
            return False

        return True

    @property
    def spread_max_delay_seconds(self) -> int:
        """Max seconds an exit may be deferred due to wide spreads."""
        return self._spread_max_delay_s

    # ------------------------------------------------------------------
    # Emergency exit factory
    # ------------------------------------------------------------------

    @staticmethod
    def make_emergency_exit(
        reason: ExitReason, current_price: float
    ) -> ExitDecision:
        """Create an emergency exit decision.

        Used for kill switch, daily loss limit, and drawdown breaker --
        always uses market orders.
        """
        return ExitDecision(
            should_exit=True,
            reason=reason,
            is_emergency=True,
            exit_price=current_price,
            use_market_order=True,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _time_stop_should_use_market(
        self, hold_type: HoldType, pnl_pct: float
    ) -> bool:
        """Determine order type for a time stop based on P&L.

        Intraday (4h):
        - P&L between -0.5% and +0.5%: market (going nowhere)
        - P&L between -2% and -0.5%: keep stop, give extra hour (return False
          for now; the caller handles the extended window)
        - P&L > +0.5% but < trailing activation: limit at bid

        Swing (5 days):
        - P&L > 0: limit at bid
        - P&L 0 to -1.5%: tighten stop (return False; caller handles)
        - P&L < -1.5%: market
        """
        if hold_type == HoldType.INTRADAY:
            params: dict[str, Any] = self.get_exit_params(HoldType.INTRADAY)
            flat_threshold: float = float(
                params.get("time_stop_flat_threshold", 0.005)
            )
            if abs(pnl_pct) <= flat_threshold:
                return True  # Flat -- use market to get out
            if pnl_pct > flat_threshold:
                return False  # Positive -- use limit at bid
            # Negative but above stop: could give more time, but we close
            return True
        else:
            # Swing
            if pnl_pct > 0:
                return False  # Limit at bid
            if pnl_pct >= -0.015:
                return False  # Tighten stop; caller manages
            return True  # Deep loss -- market

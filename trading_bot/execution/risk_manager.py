"""Centralized risk management with circuit breakers and kill switch.

Enforces daily loss limits, position caps, sector exposure, correlation
checks, drawdown breakers, and commission budgets.  All checks consult
the current phase via Config.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import deque
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from trading_bot.constants import (
    GICS_SECTOR,
    ExitReason,
    Phase,
    PositionStatus,
)

if TYPE_CHECKING:
    from trading_bot.config import Config
    from trading_bot.data.fx import FXManager
    from trading_bot.data.market_data import MarketDataManager
    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")


class RiskManager:
    """Enforces all risk limits and circuit breakers.

    Tracks daily P&L, trade count, order rejections, and drawdown state.
    Exposes ``can_trade()`` as the single top-level gate that order
    placement must pass before opening new positions.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        config: Config,
        db_path: str,
        fx: FXManager,
        notifier: Notifier,
    ) -> None:
        self._config: Config = config
        self._db_path: str = db_path
        self._fx: FXManager = fx
        self._notifier: Notifier = notifier

        # Daily counters (reset each trading day via reset_daily)
        self._daily_pnl_gbp: float = 0.0
        self._daily_gross_pnl_gbp: float = 0.0
        self._daily_commissions_gbp: float = 0.0
        self._trade_count: int = 0
        self._trading_day: date = datetime.now(tz=ET).date()

        # Pause / circuit-breaker state
        self._is_paused: bool = False
        self._pause_reason: str | None = None
        self._pause_until: datetime | None = None

        # Drawdown breaker recovery tracking
        self._drawdown_breaker_active: bool = False
        self._recovery_trades_remaining: int = 0
        self._recovery_size_pct: float = 1.0

        # Order rejection tracking (sliding window)
        self._recent_rejections: deque[float] = deque()

        # Commission budget state
        self._commission_cooldown_until: datetime | None = None

    # ------------------------------------------------------------------
    # Top-level gate
    # ------------------------------------------------------------------

    def can_trade(self) -> tuple[bool, str | None]:
        """Check whether opening a new position is currently allowed.

        Returns ``(True, None)`` if trading is permitted, or
        ``(False, reason)`` if blocked.
        """
        # Explicit pause (kill switch, drawdown breaker pause day, etc.)
        if self._is_paused:
            if self._pause_until is not None:
                now: datetime = datetime.now(tz=ET)
                if now >= self._pause_until:
                    self.resume_trading()
                else:
                    return False, self._pause_reason
            else:
                return False, self._pause_reason

        # Daily loss limit
        # Note: we cannot get live equity here, so the main loop must call
        # check_daily_loss_limit() with current P&L before calling can_trade().
        # We store the result as a flag.
        if self._daily_loss_limit_hit:
            return False, "Daily loss limit breached"

        # Max daily trades
        if self.check_daily_trade_count():
            return False, (
                f"Max daily trades reached ({self._trade_count}/"
                f"{self._config.get_max_daily_trades()})"
            )

        # Order rejection pause
        if self.check_order_rejections():
            return False, "Paused due to excessive order rejections"

        # Commission budget stop
        if self._commission_stop_active:
            return False, "Commission budget stop (>50% of gross P&L)"

        # Commission cooldown (elevated cooldown from warning)
        if self._commission_cooldown_until is not None:
            now = datetime.now(tz=ET)
            if now < self._commission_cooldown_until:
                return False, (
                    f"Commission cooldown until "
                    f"{self._commission_cooldown_until.strftime('%H:%M')}"
                )
            self._commission_cooldown_until = None

        return True, None

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def check_daily_loss_limit(
        self,
        current_pnl_gbp: float,
        account_equity_gbp: float,
    ) -> bool:
        """Return ``True`` if the daily loss limit (-1% of equity) is breached.

        The caller should supply realised + unrealised P&L for the day.
        """
        limit: float = account_equity_gbp * self._config.daily_loss_limit_pct
        breached: bool = current_pnl_gbp <= -limit
        if breached and not self._daily_loss_limit_hit:
            self._daily_loss_limit_hit = True
            logger.warning(
                "Daily loss limit breached: P&L GBP%.2f <= -%.2f (%.1f%% of equity)",
                current_pnl_gbp,
                limit,
                self._config.daily_loss_limit_pct * 100,
            )
        return breached

    @property
    def _daily_loss_limit_hit(self) -> bool:
        return getattr(self, "_loss_limit_flag", False)

    @_daily_loss_limit_hit.setter
    def _daily_loss_limit_hit(self, value: bool) -> None:
        self._loss_limit_flag: bool = value

    def check_max_positions(self, current_count: int) -> bool:
        """Return ``True`` if max concurrent positions reached."""
        max_pos: int = self._config.get_max_positions()
        if current_count >= max_pos:
            logger.debug(
                "Max positions reached: %d/%d", current_count, max_pos,
            )
            return True
        return False

    def check_sector_exposure(
        self,
        sector: str,
        current_positions: list[dict[str, Any]],
    ) -> bool:
        """Return ``True`` if sector exposure limit reached for *sector*.

        *current_positions* is a list of dicts each with at least a
        ``'ticker'`` or ``'sector'`` key.
        """
        max_sector: int = self._config.get_max_sector_exposure()
        count: int = 0
        for pos in current_positions:
            pos_sector: str = pos.get("sector", "") or GICS_SECTOR.get(
                pos.get("ticker", ""), ""
            )
            if pos_sector == sector:
                count += 1

        if count >= max_sector:
            logger.debug(
                "Sector exposure limit reached for %s: %d/%d",
                sector,
                count,
                max_sector,
            )
            return True
        return False

    def check_correlation(
        self,
        ticker: str,
        current_positions: list[dict[str, Any]],
        market_data: MarketDataManager,
    ) -> bool:
        """Return ``True`` if *ticker* correlates > threshold with any held position.

        Uses 30-day daily return correlation from cached historical data.
        If correlation data is unavailable, allows the trade (conservative
        in the "don't block" direction when data is missing).
        """
        threshold: float = self._config.correlation_threshold

        for pos in current_positions:
            held_ticker: str = pos.get("ticker", "")
            if not held_ticker:
                continue

            try:
                correlation: float | None = self._compute_correlation(
                    ticker, held_ticker, market_data,
                )
            except Exception:
                logger.warning(
                    "Correlation check failed for %s vs %s, allowing trade",
                    ticker,
                    held_ticker,
                    exc_info=True,
                )
                continue

            if correlation is not None and abs(correlation) > threshold:
                logger.info(
                    "Blocked %s - correlation %.3f with held %s (threshold %.2f)",
                    ticker,
                    correlation,
                    held_ticker,
                    threshold,
                )
                return True

        return False

    def _compute_correlation(
        self,
        ticker_a: str,
        ticker_b: str,
        market_data: MarketDataManager,
    ) -> float | None:
        """Compute 30-day daily return correlation between two tickers.

        Returns ``None`` if insufficient data.  Uses historical close
        prices from MarketDataManager cache.
        """
        try:
            closes_a: list[float] | None = getattr(
                market_data, "get_daily_closes", lambda t, n: None
            )(ticker_a, 30)
            closes_b: list[float] | None = getattr(
                market_data, "get_daily_closes", lambda t, n: None
            )(ticker_b, 30)
        except Exception:
            return None

        if closes_a is None or closes_b is None:
            return None

        min_len: int = min(len(closes_a), len(closes_b))
        if min_len < 10:
            return None

        # Align to same length
        a: list[float] = closes_a[-min_len:]
        b: list[float] = closes_b[-min_len:]

        # Compute daily returns
        returns_a: list[float] = [
            (a[i] - a[i - 1]) / a[i - 1] for i in range(1, len(a)) if a[i - 1] != 0
        ]
        returns_b: list[float] = [
            (b[i] - b[i - 1]) / b[i - 1] for i in range(1, len(b)) if b[i - 1] != 0
        ]

        n: int = min(len(returns_a), len(returns_b))
        if n < 5:
            return None

        ra: list[float] = returns_a[-n:]
        rb: list[float] = returns_b[-n:]

        mean_a: float = sum(ra) / n
        mean_b: float = sum(rb) / n

        cov: float = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n)) / n
        std_a: float = (sum((x - mean_a) ** 2 for x in ra) / n) ** 0.5
        std_b: float = (sum((x - mean_b) ** 2 for x in rb) / n) ** 0.5

        if std_a == 0 or std_b == 0:
            return None

        return cov / (std_a * std_b)

    def check_daily_trade_count(self) -> bool:
        """Return ``True`` if max daily trade count reached."""
        max_trades: int = self._config.get_max_daily_trades()
        return self._trade_count >= max_trades

    def check_drawdown_breaker(self, account_equity_gbp: float) -> bool:
        """Return ``True`` if 5-day rolling drawdown exceeds 5% from peak.

        Reads the last N days of equity from ``daily_summaries`` to find
        the rolling peak.  If the breaker fires, it pauses trading for
        one day and activates the recovery-size regime.
        """
        rolling_days: int = self._config.drawdown_breaker_rolling_days
        threshold_pct: float = self._config.drawdown_breaker_threshold

        peak_equity: float = self._get_rolling_peak_equity(
            rolling_days, account_equity_gbp,
        )

        if peak_equity <= 0:
            return False

        drawdown_pct: float = (peak_equity - account_equity_gbp) / peak_equity

        if drawdown_pct >= threshold_pct:
            logger.warning(
                "Drawdown breaker triggered: %.2f%% from 5-day peak "
                "(peak=GBP%.2f, current=GBP%.2f)",
                drawdown_pct * 100,
                peak_equity,
                account_equity_gbp,
            )
            self._activate_drawdown_breaker(drawdown_pct)
            return True

        return False

    def _get_rolling_peak_equity(
        self,
        rolling_days: int,
        current_equity: float,
    ) -> float:
        """Read equity values from daily_summaries for the last N days."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                cutoff: str = (
                    date.today() - timedelta(days=rolling_days)
                ).isoformat()
                rows = conn.execute(
                    "SELECT account_equity_gbp FROM daily_summaries "
                    "WHERE date >= ? ORDER BY date DESC",
                    (cutoff,),
                ).fetchall()
                equities: list[float] = [float(r[0]) for r in rows]
            finally:
                conn.close()
        except (sqlite3.OperationalError, Exception):
            logger.debug("Could not read daily_summaries for drawdown check")
            equities = []

        # Include current equity in the peak calculation
        equities.append(current_equity)
        return max(equities) if equities else current_equity

    def _activate_drawdown_breaker(self, drawdown_pct: float) -> None:
        """Activate the drawdown circuit breaker."""
        pause_days: int = self._config.drawdown_breaker_pause_days
        recovery_pct: float = float(
            self._config._get(
                "risk", "drawdown_breaker", "recovery_position_size_pct",
                default=0.50,
            )
        )
        recovery_trades: int = int(
            self._config._get(
                "risk", "drawdown_breaker", "recovery_trades",
                default=3,
            )
        )

        self._drawdown_breaker_active = True
        self._recovery_trades_remaining = recovery_trades
        self._recovery_size_pct = recovery_pct

        # Pause for N trading days.  Approximate by calendar days + buffer.
        pause_until: datetime = datetime.now(tz=ET) + timedelta(days=pause_days + 1)
        # Adjust to next 07:00 ET (bot start time)
        pause_until = pause_until.replace(hour=7, minute=0, second=0, microsecond=0)
        self.pause_trading(
            f"Drawdown breaker: {drawdown_pct:.1%} from 5-day peak. "
            f"Paused until {pause_until.strftime('%Y-%m-%d %H:%M ET')}",
        )
        self._pause_until = pause_until

        # Fire-and-forget notification
        asyncio.ensure_future(
            self._notifier.drawdown_alert(drawdown_pct, pause_days)
        )

    def check_order_rejections(self) -> bool:
        """Return ``True`` if too many order rejections (>3 in 10 min).

        If triggered, pauses trading for 15 minutes.
        """
        window_minutes: int = int(
            self._config._get(
                "risk", "order_rejections", "window_minutes", default=10,
            )
        )
        max_count: int = int(
            self._config._get(
                "risk", "order_rejections", "max_count", default=3,
            )
        )
        pause_minutes: int = int(
            self._config._get(
                "risk", "order_rejections", "pause_minutes", default=15,
            )
        )

        now: float = time.monotonic()
        cutoff: float = now - (window_minutes * 60)

        # Purge old entries
        while self._recent_rejections and self._recent_rejections[0] < cutoff:
            self._recent_rejections.popleft()

        if len(self._recent_rejections) > max_count:
            # Already triggered, check if pause has expired
            if self._pause_until is not None:
                if datetime.now(tz=ET) >= self._pause_until:
                    self._recent_rejections.clear()
                    return False
                return True

            # Trigger the pause
            pause_until = datetime.now(tz=ET) + timedelta(minutes=pause_minutes)
            self.pause_trading(
                f"Excessive order rejections ({len(self._recent_rejections)} "
                f"in {window_minutes} min). Paused until "
                f"{pause_until.strftime('%H:%M ET')}",
            )
            self._pause_until = pause_until

            asyncio.ensure_future(
                self._notifier.send(
                    "Order Rejections",
                    f"{len(self._recent_rejections)} rejections in {window_minutes} min. "
                    f"Pausing new entries for {pause_minutes} min.",
                    priority=4,
                    tags=["warning"],
                )
            )
            return True

        return False

    def record_rejection(self, ticker: str, reason: str) -> None:
        """Record an order rejection for the sliding-window counter."""
        self._recent_rejections.append(time.monotonic())
        logger.warning("Order rejection recorded: %s - %s", ticker, reason)

        # Persist to DB
        try:
            now_str: str = datetime.now(tz=ET).isoformat()
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "INSERT INTO order_rejections "
                    "(ticker, exchange, order_type, reason, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (ticker, "", "ENTRY", reason, now_str),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to persist order rejection")

    def check_commission_budget(
        self,
        daily_commissions: float,
        daily_gross_pnl: float,
    ) -> str | None:
        """Check commission ratio.  Returns ``'warning'``, ``'stop'``, or ``None``.

        Warning fires at 20% of gross P&L, stop at 50%.
        """
        warning_ratio: float = float(
            self._config._get("risk", "commission_budget", "warning_ratio", default=0.20)
        )
        stop_ratio: float = float(
            self._config._get("risk", "commission_budget", "stop_ratio", default=0.50)
        )
        cooldown_min: int = int(
            self._config._get(
                "risk", "commission_budget", "elevated_cooldown_minutes", default=60,
            )
        )

        if daily_gross_pnl <= 0:
            # If gross P&L is zero or negative, commissions are 100%+ of PnL.
            # Only trigger stop if there's been meaningful trading.
            if daily_commissions > 0 and self._trade_count >= 3:
                self._commission_stop_active = True
                logger.warning(
                    "Commission budget stop: commissions GBP%.2f with "
                    "non-positive gross P&L GBP%.2f",
                    daily_commissions,
                    daily_gross_pnl,
                )
                return "stop"
            return None

        ratio: float = daily_commissions / daily_gross_pnl

        if ratio >= stop_ratio:
            self._commission_stop_active = True
            logger.warning(
                "Commission budget STOP: ratio %.1f%% (commissions GBP%.2f / "
                "gross P&L GBP%.2f)",
                ratio * 100,
                daily_commissions,
                daily_gross_pnl,
            )
            return "stop"

        if ratio >= warning_ratio:
            # Elevate cooldown
            self._commission_cooldown_until = datetime.now(tz=ET) + timedelta(
                minutes=cooldown_min
            )
            logger.info(
                "Commission budget WARNING: ratio %.1f%% - cooldown elevated to %d min",
                ratio * 100,
                cooldown_min,
            )
            return "warning"

        return None

    @property
    def _commission_stop_active(self) -> bool:
        return getattr(self, "_comm_stop_flag", False)

    @_commission_stop_active.setter
    def _commission_stop_active(self, value: bool) -> None:
        self._comm_stop_flag: bool = value

    # ------------------------------------------------------------------
    # Trade recording
    # ------------------------------------------------------------------

    def record_trade(self, pnl_gbp: float, commission_gbp: float) -> None:
        """Record a completed trade for daily tracking."""
        self._trade_count += 1
        self._daily_pnl_gbp += pnl_gbp
        self._daily_gross_pnl_gbp += abs(pnl_gbp) + commission_gbp
        self._daily_commissions_gbp += commission_gbp

        logger.info(
            "Trade recorded: P&L GBP%.2f, commission GBP%.2f "
            "(daily: count=%d, net_pnl=GBP%.2f)",
            pnl_gbp,
            commission_gbp,
            self._trade_count,
            self._daily_pnl_gbp,
        )

        # Decrement recovery trades counter if drawdown breaker is active
        if self._drawdown_breaker_active and self._recovery_trades_remaining > 0:
            if pnl_gbp > 0:
                self._recovery_trades_remaining -= 1
                logger.info(
                    "Drawdown recovery: %d profitable trades remaining "
                    "at reduced size",
                    self._recovery_trades_remaining,
                )
                if self._recovery_trades_remaining <= 0:
                    self._drawdown_breaker_active = False
                    self._recovery_size_pct = 1.0
                    logger.info("Drawdown recovery complete - normal sizing resumed")

    # ------------------------------------------------------------------
    # Daily lifecycle
    # ------------------------------------------------------------------

    def reset_daily(self) -> None:
        """Reset daily counters (called at start of each trading day)."""
        self._daily_pnl_gbp = 0.0
        self._daily_gross_pnl_gbp = 0.0
        self._daily_commissions_gbp = 0.0
        self._trade_count = 0
        self._trading_day = datetime.now(tz=ET).date()
        self._daily_loss_limit_hit = False
        self._commission_stop_active = False
        self._commission_cooldown_until = None
        self._recent_rejections.clear()

        logger.info("Risk manager daily counters reset for %s", self._trading_day)

    # ------------------------------------------------------------------
    # Pause / resume
    # ------------------------------------------------------------------

    def pause_trading(self, reason: str) -> None:
        """Pause all new entries."""
        self._is_paused = True
        self._pause_reason = reason
        logger.warning("Trading PAUSED: %s", reason)

    def resume_trading(self) -> None:
        """Resume trading after a pause."""
        if self._is_paused:
            logger.info(
                "Trading RESUMED (was paused: %s)", self._pause_reason,
            )
        self._is_paused = False
        self._pause_reason = None
        self._pause_until = None

    # ------------------------------------------------------------------
    # Kill switch
    # ------------------------------------------------------------------

    async def handle_kill_switch(self, gateway: GatewayConnection) -> None:
        """Kill switch: flatten all positions immediately.

        1. Cancel all pending orders.
        2. Market-sell all positions.
        3. Send confirmation via ntfy.
        4. Enter permanent close-only mode.
        """
        logger.critical("KILL SWITCH ACTIVATED")

        # Close all positions and cancel all orders via Alpaca API
        try:
            gateway.client.close_all_positions(cancel_orders=True)
            logger.info("Kill switch: closed all positions and cancelled all orders")
        except Exception:
            logger.exception("Kill switch: error flattening positions")

        # 3. Send confirmation
        try:
            await self._notifier.kill_switch(
                "Kill switch executed. All orders cancelled, "
                "all positions flattened with market orders."
            )
        except Exception:
            logger.exception("Kill switch: failed to send notification")

        # 4. Permanent close-only mode
        self.pause_trading("Kill switch activated - permanent close-only mode")
        self._pause_until = None  # No auto-resume

        logger.critical("Kill switch execution complete")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_paused(self) -> bool:
        """Whether trading is currently paused."""
        return self._is_paused

    @property
    def pause_reason(self) -> str | None:
        return self._pause_reason

    @property
    def daily_pnl_gbp(self) -> float:
        return self._daily_pnl_gbp

    @property
    def daily_commissions_gbp(self) -> float:
        return self._daily_commissions_gbp

    @property
    def trade_count(self) -> int:
        return self._trade_count

    @property
    def drawdown_breaker_active(self) -> bool:
        return self._drawdown_breaker_active

    @property
    def recovery_size_pct(self) -> float:
        """Position size multiplier during drawdown recovery.

        Returns 1.0 when normal, or the recovery fraction (e.g. 0.5)
        when the drawdown breaker is active and recovery trades remain.
        """
        if self._drawdown_breaker_active and self._recovery_trades_remaining > 0:
            return self._recovery_size_pct
        return 1.0

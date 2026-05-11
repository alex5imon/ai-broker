"""Order placement and lifecycle management via Alpaca Trading API.

Handles entry limit orders, stop-loss, take-profit, trailing stops,
partial fills, timeouts, and emergency flattening.

In the tick model (Phase 3) entry timeouts are evaluated on each tick by
comparing ``order.submitted_at`` against ``entry_timeout_seconds``; the
old ``asyncio.create_task`` per-order timer is gone because it cannot
survive a stateless cron run.  ``_active_orders`` is hydrated from the
``positions`` table at the start of ``_check_order_statuses``.

Mypy note: the alpaca-py client returns ``Order | RawData`` and
``list[Order] | RawData`` (where ``RawData = dict[str, Any]``) — the
dict path is only reachable with ``raw_data=True`` which we never
pass. Each affected SDK call site has a per-line
``# type: ignore[arg-type]`` (or ``[union-attr]``) annotation; the
file-level suppress was avoided so genuinely-buggy union accesses in
unrelated code paths still surface.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
    TrailingStopOrderRequest,
)

from trading_bot.constants import (
    TZ_EASTERN,
    ExitReason,
    PositionStatus,
)
from trading_bot.db import repository as repo
from trading_bot.gateway.order_tif import tif_for_market, tif_for_stop

if TYPE_CHECKING:
    from trading_bot.config import Config
    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


# Callback fired when a strategy-driven exit order (place_exit /
# place_limit_exit) actually FILLS at the broker. Args:
# (strategy_id, ticker, filled_qty, realized_pnl). The pnl is computed
# from the real broker fill price, so the loss-cooldown tracker sees
# slippage-true outcomes rather than signal-time mid-price estimates.
# Bracket-leg exits (stop/target/trail) intentionally do not fire this
# callback today — see item #9 follow-up in risk-infrastructure-gaps.
ExitFillCallback = Callable[[str, str, float, float], None]


# ---------------------------------------------------------------------------
# Decision dataclass — passed in from the strategy layer
# ---------------------------------------------------------------------------

@dataclass
class EntryDecision:
    """All information needed to place an entry order."""

    ticker: str
    exchange: str
    side: str                 # "BUY"
    shares: float             # supports fractional (Alpaca min increment 1e-6)
    limit_price: float
    stop_price: float         # Stop-loss price
    target_price: float       # Take-profit price
    hold_type: str            # "intraday" or "swing"
    sector: str
    phase: int
    sentiment_score: float | None
    signals: str              # JSON or descriptive string of signals
    currency: str             # "USD"
    strategy_id: str | None = None
    # Trailing-stop parameters (optional). ``trail_pct`` is the trail distance;
    # ``trail_activation_price`` is the price the position must reach before
    # the trailing stop replaces the take-profit. If ``trail_pct`` is None,
    # no trailing behaviour is installed.
    trail_pct: float | None = None
    trail_activation_price: float | None = None


# ---------------------------------------------------------------------------
# Active order tracking
# ---------------------------------------------------------------------------

@dataclass
class _ActiveOrder:
    """Internal tracking for a live order's state."""

    trade_id: int
    ticker: str
    exchange: str
    alpaca_entry_order_id: str | None = None
    alpaca_stop_order_id: str | None = None
    alpaca_target_order_id: str | None = None
    alpaca_trail_order_id: str | None = None
    # Strategy-driven exit (place_exit / place_limit_exit). Populated
    # when the position transitions to CLOSING; polled by
    # _check_order_statuses to advance CLOSING -> CLOSED with the real
    # exit_reason instead of leaving the row for state-recovery to
    # tag as 'reconciliation_mismatch' next tick. In-memory only —
    # process restarts fall back to the recovery path.
    alpaca_exit_order_id: str | None = None
    exit_reason: str | None = None
    status: PositionStatus = PositionStatus.ENTRY_PENDING
    entry_shares: float = 0.0  # float to support fractional shares
    filled_shares: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    hold_type: str = "intraday"
    strategy_id: str | None = None
    trail_pct: float | None = None
    trail_activation_price: float | None = None
    trail_activated: bool = False
    # Trades-table primary key for this position. Populated when the row is
    # inserted in _create_position_record and rehydrated via (ticker,
    # entry_time) match. Used as the WHERE on UPDATE trades during exit so
    # the update lands on the correct row — pre-Phase-3, the code reused
    # trade_id (which is positions.id) and silently missed.
    db_trade_id: int | None = None


# ---------------------------------------------------------------------------
# Order Manager
# ---------------------------------------------------------------------------

class OrderManager:
    """Places and manages orders via Alpaca Trading API.

    Handles entry, stop/target placement on fill, trailing stop
    activation, timeout cancellation, and emergency flattening.
    """

    def __init__(
        self,
        gateway: GatewayConnection,
        config: Config,
        notifier: Notifier,
        db_path: str,
    ) -> None:
        self._gw: GatewayConnection = gateway
        self._config: Config = config
        self._notifier: Notifier = notifier
        self._db_path: str = db_path

        self._active_orders: dict[int, _ActiveOrder] = {}
        self._alpaca_to_trade: dict[str, int] = {}
        # Holds positions.id -> trades.id between _create_position_record
        # and place_entry constructing the _ActiveOrder. Cleared once the
        # tracker takes ownership.
        self._pending_db_trade_ids: dict[int, int] = {}
        # Optional observer fired on strategy-driven exit fills, with the
        # actual broker fill price. StrategyManager wires this to the
        # loss-cooldown tracker so slippage is reflected in the streak.
        self._exit_fill_callback: ExitFillCallback | None = None

    def set_exit_fill_callback(self, callback: ExitFillCallback | None) -> None:
        """Register the strategy-exit fill observer (idempotent)."""
        self._exit_fill_callback = callback

    # ------------------------------------------------------------------
    # Tick-model hydration + status check
    # ------------------------------------------------------------------

    def _hydrate_active_orders(self) -> None:
        """Rebuild ``_active_orders`` from the ``positions`` table.

        Each tick starts with an empty in-memory dict; we rehydrate from
        the DB so ``_check_order_statuses`` and entry-timeout sweeps can
        operate on the same set of positions the prior tick left open.
        """
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM positions "
                    "WHERE status NOT IN (?, ?)",
                    (
                        PositionStatus.CLOSED.value,
                        PositionStatus.ENTRY_FAILED.value,
                    ),
                ).fetchall()
                # Pair-key index for trades.id lookup. Same pairing the
                # alpaca_backfill tool uses (ticker, entry_time) since
                # there is no FK between the tables.
                trades_index: dict[tuple[str, str], int] = {
                    (str(t["ticker"]), str(t["entry_time"])): int(t["id"])
                    for t in conn.execute(
                        "SELECT id, ticker, entry_time FROM trades"
                    ).fetchall()
                }
            finally:
                conn.close()
        except sqlite3.OperationalError:
            logger.warning("positions table not found during hydration")
            return

        for row in rows:
            trade_id = int(row["id"])
            if trade_id in self._active_orders:
                continue
            status_str: str = str(row["status"])
            try:
                status: PositionStatus = PositionStatus(status_str)
            except ValueError:
                logger.debug("Skipping row with unknown status %s", status_str)
                continue

            active = _ActiveOrder(
                trade_id=trade_id,
                ticker=str(row["ticker"]),
                exchange=str(row["exchange"]),
                alpaca_entry_order_id=row["alpaca_order_id"],
                alpaca_stop_order_id=row["alpaca_stop_order_id"],
                alpaca_target_order_id=row["alpaca_target_order_id"],
                alpaca_trail_order_id=row["alpaca_trail_order_id"],
                status=status,
                entry_shares=float(row["quantity"] or 0),
                filled_shares=float(row["quantity"] or 0)
                if status != PositionStatus.ENTRY_PENDING
                else 0.0,
                entry_price=float(row["entry_price"] or 0),
                stop_price=float(row["stop_price"] or 0),
                target_price=float(row["target_price"] or 0),
                hold_type=str(row["hold_type"]),
                strategy_id=row["strategy_id"],
                db_trade_id=trades_index.get(
                    (str(row["ticker"]), str(row["entry_time"]))
                ),
            )
            self._active_orders[trade_id] = active
            for oid in (
                active.alpaca_entry_order_id,
                active.alpaca_stop_order_id,
                active.alpaca_target_order_id,
                active.alpaca_trail_order_id,
            ):
                if oid is not None:
                    self._alpaca_to_trade[str(oid)] = trade_id

    def _sweep_uninitiated_pending_entries(self, timeout_seconds: int) -> None:
        """Mark ghost ENTRY_PENDING rows as ENTRY_FAILED.

        A row with ``status='ENTRY_PENDING'`` and ``alpaca_order_id IS
        NULL`` is the survivor of a crash between the
        ``_create_position_record`` INSERT and the ``submit_order`` call.
        The normal timeout sweep at ``_check_order_statuses`` keys off
        ``alpaca_entry_order_id`` and would skip these rows forever,
        permanently consuming a position slot.

        Apply a generous 2× timeout window to avoid racing a slow Alpaca
        round-trip on a concurrent tick. Anything older is either crashed
        or stuck and is safe to fail.
        """
        cutoff: datetime = datetime.now(tz=ET) - timedelta(
            seconds=timeout_seconds * 2,
        )
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, ticker, entry_time FROM positions "
                    "WHERE status = ? AND alpaca_order_id IS NULL",
                    (PositionStatus.ENTRY_PENDING.value,),
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            logger.warning(
                "uninitiated-entry sweep: positions table unreadable",
                exc_info=True,
            )
            return

        for row in rows:
            entry_time_raw = row["entry_time"]
            if not entry_time_raw:
                continue
            try:
                entry_time: datetime = datetime.fromisoformat(str(entry_time_raw))
            except ValueError:
                logger.warning(
                    "uninitiated-entry sweep: unparseable entry_time %r "
                    "for trade_id=%d",
                    entry_time_raw, int(row["id"]),
                )
                continue
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=ET)
            if entry_time > cutoff:
                # Recent — submit may still be in flight. Skip.
                continue

            trade_id: int = int(row["id"])
            logger.warning(
                "uninitiated-entry sweep: marking trade_id=%d ticker=%s "
                "ENTRY_FAILED (status=ENTRY_PENDING but alpaca_order_id "
                "is NULL; entry_time=%s, age > %ds)",
                trade_id, row["ticker"], entry_time.isoformat(),
                timeout_seconds * 2,
            )
            self._update_position_status(trade_id, PositionStatus.ENTRY_FAILED)
            # Drop from the in-memory map if hydration loaded it.
            self._active_orders.pop(trade_id, None)

    async def _check_order_statuses(self) -> None:
        """Check status of all tracked orders via Alpaca API.

        Rehydrates ``_active_orders`` from the DB (tick model) and, for
        each ENTRY_PENDING order, also enforces the configured entry
        timeout using Alpaca's ``submitted_at`` timestamp.
        """
        self._hydrate_active_orders()

        client: TradingClient = self._gw.client
        timeout_seconds: int = int(
            self._config._get("entry", "entry_timeout_seconds", default=300)
        )
        self._sweep_uninitiated_pending_entries(timeout_seconds)
        min_fill_pct: float = float(
            self._config._get("entry", "partial_fill_min_pct", default=0.50)
        )
        now: datetime = datetime.now(tz=ET)

        for trade_id, active in list(self._active_orders.items()):
            # Check entry order
            if (
                active.status == PositionStatus.ENTRY_PENDING
                and active.alpaca_entry_order_id
            ):
                try:
                    order: AlpacaOrder = await asyncio.to_thread(
                        client.get_order_by_id,  # type: ignore[arg-type]
                        active.alpaca_entry_order_id,
                    )
                    if order.status.value == "filled":
                        active.filled_shares = float(order.filled_qty or 0)
                        active.entry_price = float(order.filled_avg_price or 0)
                        logger.info(
                            "Entry FILLED: %s %.4f @ %.4f (trade_id=%d)",
                            active.ticker, active.filled_shares,
                            active.entry_price, trade_id,
                        )
                        await self._transition_to_open(trade_id, active.filled_shares)

                    elif order.status.value in ("canceled", "expired", "rejected"):
                        logger.info(
                            "Entry %s: %s (trade_id=%d)",
                            order.status.value, active.ticker, trade_id,
                        )
                        # Phase 3 B2: Use ENTRY_FAILED, not CLOSED — no fill
                        # ever happened so this is not a real round-trip.
                        self._update_position_status(
                            trade_id, PositionStatus.ENTRY_FAILED
                        )
                        active.status = PositionStatus.ENTRY_FAILED
                        self._log_rejection(
                            ticker=active.ticker,
                            exchange=active.exchange,
                            order_type="ENTRY",
                            intended_price=active.entry_price,
                            intended_qty=active.entry_shares,
                            reason=f"alpaca_{order.status.value}",
                        )
                        del self._active_orders[trade_id]

                    elif order.status.value == "partially_filled":
                        active.filled_shares = float(order.filled_qty or 0)
                        await self._maybe_timeout_entry(
                            trade_id, active, order,
                            now=now,
                            timeout_seconds=timeout_seconds,
                            min_fill_pct=min_fill_pct,
                        )
                    else:
                        # Still open/accepted/new — check age-based timeout.
                        await self._maybe_timeout_entry(
                            trade_id, active, order,
                            now=now,
                            timeout_seconds=timeout_seconds,
                            min_fill_pct=min_fill_pct,
                        )

                except Exception:
                    logger.warning(
                        "Error checking entry order for %s", active.ticker,
                        exc_info=True,
                    )

            # Check exit orders (stop/target/trail)
            if active.status in (
                PositionStatus.STOP_AND_TARGET_ACTIVE,
                PositionStatus.TRAILING_ACTIVE,
            ):
                for order_id_attr, exit_reason in [
                    ("alpaca_stop_order_id", ExitReason.STOP_LOSS),
                    ("alpaca_target_order_id", ExitReason.TAKE_PROFIT),
                    ("alpaca_trail_order_id", ExitReason.TRAILING_STOP),
                ]:
                    order_id: str | None = getattr(active, order_id_attr)
                    if order_id is None:
                        continue
                    try:
                        order = await asyncio.to_thread(
                            client.get_order_by_id,  # type: ignore[arg-type]
                            order_id,
                        )
                        if order.status.value == "filled":
                            exit_price: float = float(order.filled_avg_price or 0)
                            logger.info(
                                "Exit FILLED: %s reason=%s @ %.4f (trade_id=%d)",
                                active.ticker, exit_reason.value,
                                exit_price, trade_id,
                            )
                            await self._cancel_other_exits(trade_id, active, order_id)
                            await self._close_position(
                                trade_id, active, exit_price, exit_reason.value,
                            )
                            pnl: float = (exit_price - active.entry_price) * active.filled_shares
                            await self._notifier.position_closed(
                                ticker=active.ticker, pnl=pnl,
                                hold_time="", exit_reason=exit_reason.value,
                            )
                            break
                    except Exception:
                        logger.warning(
                            "Error checking exit order for %s", active.ticker,
                            exc_info=True,
                        )

            # Strategy-driven exit (place_exit / place_limit_exit). Without
            # this branch the row stays CLOSING until the next tick's
            # StateRecovery reconciles it as 'reconciliation_mismatch',
            # losing the real exit_reason. The recovery path is still the
            # ultimate fallback for crashed ticks / process restarts (the
            # exit_order_id is in-memory only).
            if (
                active.status == PositionStatus.CLOSING
                and active.alpaca_exit_order_id is not None
            ):
                exit_oid: str = active.alpaca_exit_order_id
                try:
                    exit_order = await asyncio.to_thread(
                        client.get_order_by_id,  # type: ignore[arg-type]
                        exit_oid,
                    )
                    # Alpaca returns Order | RawData; we never request raw
                    # so the typed branch is the only reachable one. The
                    # union-attr ignores below cover the .status / .filled_avg_price
                    # accesses across this block.
                    if exit_order.status.value == "filled":  # type: ignore[union-attr]
                        exit_px: float = float(exit_order.filled_avg_price or 0)  # type: ignore[union-attr]
                        # Prefer the broker's reported filled_qty over the
                        # cached entry-fill qty so partial-fill exits (rare
                        # but possible) feed the cooldown tracker the qty
                        # that actually transacted, not the qty we sent.
                        filled_exit_qty: float = float(
                            exit_order.filled_qty or 0  # type: ignore[union-attr]
                        )
                        if filled_exit_qty <= 0:
                            filled_exit_qty = active.filled_shares
                        reason_str: str = active.exit_reason or "strategy_exit"
                        logger.info(
                            "Strategy exit FILLED: %s reason=%s @ %.4f qty=%.4f (trade_id=%d)",
                            active.ticker, reason_str, exit_px,
                            filled_exit_qty, trade_id,
                        )
                        await self._close_position(
                            trade_id, active, exit_px, reason_str,
                            filled_qty=filled_exit_qty,
                        )
                        pnl_strategy: float = (
                            (exit_px - active.entry_price) * filled_exit_qty
                        )
                        await self._notifier.position_closed(
                            ticker=active.ticker, pnl=pnl_strategy,
                            hold_time="", exit_reason=reason_str,
                        )
                        # Deferred loss-cooldown bookkeeping — keyed on the
                        # actual broker fill price, not the signal-time mid
                        # that check_exits had to estimate. Fire only on
                        # FILL; cancel/expire/reject leaves the position
                        # open and is handled by the rollback branch below
                        # without touching the cooldown streak.
                        cb: ExitFillCallback | None = self._exit_fill_callback
                        if cb is not None and active.strategy_id:
                            try:
                                cb(
                                    active.strategy_id,
                                    active.ticker,
                                    filled_exit_qty,
                                    pnl_strategy,
                                )
                            except Exception:
                                logger.exception(
                                    "exit_fill_callback raised for %s (trade_id=%d)",
                                    active.ticker, trade_id,
                                )
                    elif exit_order.status.value in ("canceled", "expired", "rejected"):  # type: ignore[union-attr]
                        # Limit exit didn't fill (e.g. price gapped through
                        # the limit). Roll back to STOP_AND_TARGET_ACTIVE so
                        # the next tick re-evaluates the exit condition; the
                        # protective stop is re-attached by recovery if it
                        # was cancelled.
                        logger.warning(
                            "Strategy exit %s for %s — rolling back to STOP_AND_TARGET_ACTIVE",
                            exit_order.status.value, active.ticker,  # type: ignore[union-attr]
                        )
                        active.alpaca_exit_order_id = None
                        active.exit_reason = None
                        active.status = PositionStatus.STOP_AND_TARGET_ACTIVE
                        self._update_position_status(
                            trade_id, PositionStatus.STOP_AND_TARGET_ACTIVE,
                        )
                except Exception:
                    logger.warning(
                        "Error polling strategy exit for %s", active.ticker,
                        exc_info=True,
                    )

    # ------------------------------------------------------------------
    # Entry orders
    # ------------------------------------------------------------------

    async def place_entry(self, decision: EntryDecision) -> int | None:
        """Place an entry limit order.  Returns the internal trade_id on success.

        Submits a *plain* ``LimitOrderRequest`` — no bracket, no children.
        Alpaca rejects fractional+bracket combos with code 42210000 and the
        whole-share floor we used to apply was catastrophic on the $1k live
        account (dropped ~50% of mean_reversion signals). The protective stop
        is attached as a standalone order in :meth:`_transition_to_open` once
        the entry confirms a fill; take-profit is managed by the tick-loop's
        ``check_exits`` polling against ``target_price``.
        """
        if decision.shares <= 0:
            logger.info(
                "[%s] Entry skipped: shares=%.6f <= 0 (price=%.2f)",
                decision.ticker, decision.shares, decision.limit_price,
            )
            return None

        trade_id: int | None = self._create_position_record(decision)
        if trade_id is None:
            logger.error("Failed to create position record for %s", decision.ticker)
            return None

        client: TradingClient = self._gw.client

        try:
            request = LimitOrderRequest(
                symbol=decision.ticker,
                qty=decision.shares,
                side=OrderSide.BUY if decision.side == "BUY" else OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(decision.limit_price, 2),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            alpaca_order_id: str = str(order.id)

            logger.info(
                "Entry order placed: %s %s %.6f @ %.2f (alpaca_id=%s, trade_id=%d)",
                decision.side, decision.ticker, decision.shares,
                decision.limit_price, alpaca_order_id, trade_id,
            )

            active: _ActiveOrder = _ActiveOrder(
                trade_id=trade_id,
                ticker=decision.ticker,
                exchange=decision.exchange,
                alpaca_entry_order_id=alpaca_order_id,
                status=PositionStatus.ENTRY_PENDING,
                entry_shares=decision.shares,
                entry_price=decision.limit_price,
                stop_price=decision.stop_price,
                target_price=decision.target_price,
                hold_type=decision.hold_type,
                strategy_id=decision.strategy_id,
                trail_pct=decision.trail_pct,
                trail_activation_price=decision.trail_activation_price,
                db_trade_id=self._pending_db_trade_ids.pop(trade_id, None),
            )
            self._active_orders[trade_id] = active
            self._alpaca_to_trade[alpaca_order_id] = trade_id

            self._update_position_field(trade_id, "alpaca_order_id", alpaca_order_id)

            return trade_id

        except Exception as exc:
            logger.exception("Failed to place entry order for %s", decision.ticker)
            # Phase 3 B2: ENTRY_FAILED, not CLOSED — Alpaca rejected the
            # submit so no order ever existed. CLOSED implies a real
            # round-trip and pollutes every postmortem report.
            self._update_position_status(trade_id, PositionStatus.ENTRY_FAILED)
            # Mark the trades row as entry_failed so daily_summaries and the
            # self-improvement postmortem don't see a permanently-open ghost.
            # UPDATE rather than DELETE so the audit trail (rejection reason
            # via _log_rejection below) remains pinned to a real trades.id.
            db_trade_id: int | None = self._pending_db_trade_ids.pop(trade_id, None)
            if db_trade_id is not None:
                self._mark_trade_entry_failed(db_trade_id, decision.limit_price)
            self._log_rejection(
                ticker=decision.ticker,
                exchange=decision.exchange,
                order_type="ENTRY",
                intended_price=decision.limit_price,
                intended_qty=decision.shares,
                reason=f"alpaca_submit_error: {type(exc).__name__}: {exc}"[:200],
            )
            return None

    async def _maybe_timeout_entry(
        self,
        trade_id: int,
        active: _ActiveOrder,
        order: AlpacaOrder,
        *,
        now: datetime,
        timeout_seconds: int,
        min_fill_pct: float,
    ) -> None:
        """Cancel a stale ENTRY_PENDING order if it has exceeded timeout.

        Age is computed from Alpaca's ``submitted_at`` so the check is
        idempotent across ticks.  A partial fill above ``min_fill_pct``
        is accepted; anything less is cancelled (and any partial fill
        emergency-flattened).
        """
        submitted_at: datetime | None = getattr(order, "submitted_at", None)
        if submitted_at is None:
            return
        if submitted_at.tzinfo is None:
            submitted_at = submitted_at.replace(tzinfo=ET)

        age: timedelta = now - submitted_at
        if age.total_seconds() < timeout_seconds:
            return

        alpaca_order_id: str = str(order.id)
        min_fill_qty: float = max(0.001, active.entry_shares * min_fill_pct)

        if active.filled_shares >= min_fill_qty:
            logger.info(
                "Entry timeout: accepting partial fill %.4f/%.4f for %s (trade_id=%d)",
                active.filled_shares, active.entry_shares, active.ticker, trade_id,
            )
            await self.cancel_order(alpaca_order_id)
            await self._transition_to_open(trade_id, active.filled_shares)
        else:
            logger.info(
                "Entry timeout: cancelling %s (filled %.4f/%.4f, age=%.0fs, trade_id=%d)",
                active.ticker, active.filled_shares, active.entry_shares,
                age.total_seconds(), trade_id,
            )
            await self.cancel_order(alpaca_order_id)

            if active.filled_shares > 0:
                await self.emergency_flatten(
                    active.ticker, active.filled_shares, active.exchange,
                )

            # Phase 3 B2: zero-fill timeout cancels the order at the broker
            # so no entry ever happens — ENTRY_FAILED, not CLOSED.
            self._update_position_status(trade_id, PositionStatus.ENTRY_FAILED)
            active.status = PositionStatus.ENTRY_FAILED
            self._log_rejection(
                ticker=active.ticker,
                exchange=active.exchange,
                order_type="ENTRY",
                intended_price=active.entry_price,
                intended_qty=active.entry_shares,
                reason=f"entry_timeout: filled {active.filled_shares:.4f}/"
                       f"{active.entry_shares:.4f} after {age.total_seconds():.0f}s",
            )
            del self._active_orders[trade_id]

    # ------------------------------------------------------------------
    # Stop / Target / Trailing
    # ------------------------------------------------------------------

    # NOTE: place_stop_loss and place_take_profit were deleted in the V6
    # cleanup. Primary entry stops/targets are now placed atomically via the
    # BRACKET order class (see ``place_entry``), and grep confirmed neither
    # helper had any callers in the trading_bot package or tests.

    async def place_trailing_stop(
        self, trade_id: int, ticker: str, exchange: str,
        qty: float, trail_pct: float,
    ) -> str | None:
        """Place a trailing stop order.  Returns Alpaca order ID or None."""
        client: TradingClient = self._gw.client
        try:
            request = TrailingStopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=tif_for_stop(qty),
                trail_percent=round(trail_pct * 100, 2),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            order_id: str = str(order.id)
            logger.info(
                "Trailing stop placed: %s %.6f trail=%.2f%% (alpaca_id=%s, trade_id=%d)",
                ticker, qty, trail_pct * 100, order_id, trade_id,
            )

            active: _ActiveOrder | None = self._active_orders.get(trade_id)
            if active is not None:
                active.alpaca_trail_order_id = order_id
            self._alpaca_to_trade[order_id] = trade_id
            self._update_position_field(trade_id, "alpaca_trail_order_id", order_id)
            self._update_position_field(trade_id, "trailing_active", 1)
            self._update_position_field(trade_id, "trailing_distance", trail_pct)
            return order_id

        except Exception:
            logger.exception("Failed to place trailing stop for %s (trade_id=%d)", ticker, trade_id)
            return None

    # ------------------------------------------------------------------
    # Cancel / Flatten
    # ------------------------------------------------------------------

    async def cancel_order(self, order_id: str) -> None:
        """Cancel an order by Alpaca order ID."""
        try:
            await asyncio.to_thread(self._gw.client.cancel_order_by_id, order_id)
            logger.info("Cancelled order %s", order_id)
        except Exception:
            # Benign: order may already be filled/cancelled by the time we try.
            logger.debug("Error cancelling order %s (may already be done)", order_id, exc_info=True)

    async def cancel_all_for_ticker(self, ticker: str) -> None:
        """Cancel all open orders for a ticker."""
        try:
            from alpaca.trading.enums import QueryOrderStatus
            request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[ticker],
            )
            orders: list[AlpacaOrder] = await asyncio.to_thread(
                self._gw.client.get_orders,  # type: ignore[arg-type]
                filter=request,
            )
            for order in orders:
                try:
                    await asyncio.to_thread(
                        self._gw.client.cancel_order_by_id, str(order.id),
                    )
                except Exception:
                    logger.warning("Error cancelling order for %s", ticker, exc_info=True)
            if orders:
                logger.info("Cancelled %d order(s) for %s", len(orders), ticker)
        except Exception:
            logger.exception("Error cancelling orders for %s", ticker)

    async def emergency_flatten(self, ticker: str, qty: float, exchange: str) -> None:
        """Emergency market sell."""
        client: TradingClient = self._gw.client
        try:
            request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                time_in_force=tif_for_market(qty),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            logger.warning(
                "Emergency flatten: SELL %.6f %s @ MARKET (alpaca_id=%s)",
                qty, ticker, str(order.id),
            )
        except Exception:
            logger.exception(
                "CRITICAL: Failed emergency flatten for %s (%.6f shares)", ticker, qty,
            )
            await self._notifier.send(
                "Emergency Flatten Failed",
                f"Failed to flatten {qty} shares of {ticker}. Manual intervention required!",
                priority=5,
                tags=["skull_and_crossbones"],
            )

    async def place_exit(
        self,
        ticker: str,
        qty: float,
        reason: str,
        *,
        is_emergency: bool = False,
    ) -> str | None:
        """Submit a strategy-driven exit (market sell).

        Cancels any outstanding bracket legs for the matching position so
        the broker doesn't reject the new order with insufficient-qty.
        Returns the Alpaca order id on submission, or None on failure.
        """
        if qty <= 0:
            logger.warning(
                "place_exit called with non-positive qty for %s: %s", ticker, qty,
            )
            return None

        # Find the matching active order (DB-hydrated) so we can release
        # the bracket legs that reserve the shares. Exclude terminal-ish
        # statuses (CLOSED/ENTRY_FAILED) AND CLOSING — a position already
        # transitioning to closed has its exit order in flight; matching
        # it here would resubmit a duplicate SELL. ENTRY_FAILED rows
        # represent positions that never filled, so a market SELL would
        # open an unintended short.
        matching_trade_id: int | None = None
        matching_active: _ActiveOrder | None = None
        # If a ticker has any in-memory _ActiveOrder in a refusal status
        # (ENTRY_FAILED / CLOSING) but no eligible match, refuse — that
        # means we already submitted an exit (CLOSING) or the entry never
        # filled (ENTRY_FAILED). Drain or recovery callers should not
        # follow with another SELL.
        has_blocking_status: bool = False
        for tid, active in self._active_orders.items():
            if active.ticker != ticker:
                continue
            if active.status in (
                PositionStatus.CLOSED,
                PositionStatus.ENTRY_FAILED,
                PositionStatus.CLOSING,
            ):
                has_blocking_status = True
                continue
            matching_trade_id = tid
            matching_active = active
            break

        if matching_active is None and has_blocking_status:
            logger.warning(
                "place_exit refused for %s: existing in-memory order is "
                "ENTRY_FAILED/CLOSING/CLOSED — refusing to submit a "
                "market SELL that would short a never-filled position or "
                "duplicate an in-flight close (reason=%s)",
                ticker, reason,
            )
            return None

        # Cancel ALL open orders for the ticker, not just the locally
        # tracked bracket legs. Phantom stops (placed by earlier retry
        # paths and never linked back into the DB) hold the qty as
        # ``held_for_orders`` on Alpaca and cause the subsequent SELL
        # to fail with code 40310000 ("insufficient qty available").
        # Observed 2026-05-08 → 2026-05-11: XLK overnight_drift exit
        # blocked for 3 trading days by an untracked stop, only cleared
        # when an emergency-flatten path explicitly canceled all
        # orders for the symbol. Treat broker as source of truth.
        await self.cancel_all_for_ticker(ticker)

        client: TradingClient = self._gw.client
        try:
            request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            order_id: str = str(order.id)
            logger.info(
                "Strategy exit submitted: SELL %s %s @ MARKET (reason=%s, alpaca_id=%s)",
                qty, ticker, reason, order_id,
            )
            # Pin order_id → trade_id and transition to CLOSING so the
            # next stateless tick doesn't re-evaluate the same exit and
            # double-submit. Fill detection moves CLOSING → CLOSED.
            # Recovery/drain paths without an in-memory match still
            # benefit from the caller marking the DB row CLOSING.
            if matching_trade_id is not None:
                self._alpaca_to_trade[order_id] = matching_trade_id
                if matching_active is not None:
                    matching_active.status = PositionStatus.CLOSING
                    matching_active.alpaca_exit_order_id = order_id
                    matching_active.exit_reason = reason
                self._update_position_status(
                    matching_trade_id, PositionStatus.CLOSING,
                )
            return order_id
        except Exception:
            logger.exception(
                "CRITICAL: Failed to place strategy exit for %s (qty=%s, reason=%s)",
                ticker, qty, reason,
            )
            if is_emergency:
                await self._notifier.send(
                    "Strategy Exit Failed",
                    f"Failed to exit {qty} {ticker} (reason={reason}). Manual review required.",
                    priority=4,
                    tags=["warning"],
                )
            return None

    async def place_limit_exit(
        self,
        ticker: str,
        qty: float,
        limit_price: float,
        reason: str,
    ) -> str | None:
        """Submit a strategy-driven limit-sell exit.

        Mirrors :meth:`place_exit` but uses a DAY limit order instead of
        a market order, and — critically — transitions the matching
        position to ``CLOSING`` and persists the exit order_id BEFORE
        returning. This prevents the next stateless tick from
        re-evaluating the same exit condition and double-submitting.

        Returns the Alpaca order id on submission, or ``None`` on
        failure. On failure the caller should fall back to
        :meth:`emergency_flatten`.
        """
        if qty <= 0:
            logger.warning(
                "place_limit_exit called with non-positive qty for %s: %s",
                ticker, qty,
            )
            return None

        # Find the matching active order so we can cancel bracket legs
        # and pin the exit order_id back to the trade. Also exclude
        # CLOSING — a position already transitioning to closed has its
        # exit order in flight; matching here would resubmit a duplicate.
        matching_trade_id: int | None = None
        matching_active: _ActiveOrder | None = None
        has_blocking_status: bool = False
        for tid, active in self._active_orders.items():
            if active.ticker != ticker:
                continue
            if active.status in (
                PositionStatus.CLOSED,
                PositionStatus.ENTRY_FAILED,
                PositionStatus.CLOSING,
            ):
                has_blocking_status = True
                continue
            matching_trade_id = tid
            matching_active = active
            break

        if matching_active is None and has_blocking_status:
            logger.warning(
                "place_limit_exit refused for %s: existing in-memory "
                "order is ENTRY_FAILED/CLOSING/CLOSED (reason=%s)",
                ticker, reason,
            )
            return None

        # Cancel ALL open orders for the ticker. Mirrors the change in
        # place_exit — see that comment for rationale. Phantom stops not
        # tracked in the local DB will otherwise hold the qty and reject
        # this LIMIT submission with "insufficient qty available".
        await self.cancel_all_for_ticker(ticker)

        # Snapshot the prior status so we can roll back if Alpaca
        # rejects the submission. Without this, a failed submit leaves
        # the in-memory _ActiveOrder/DB row in CLOSING with no
        # corresponding alpaca order_id — fill detection never advances
        # CLOSING → CLOSED and the row leaks open forever.
        prior_status: PositionStatus | None = (
            matching_active.status if matching_active is not None else None
        )

        client: TradingClient = self._gw.client
        order_id: str | None = None
        try:
            request = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            order_id = str(order.id)
            logger.info(
                "Limit exit submitted: SELL %s %s @ %.2f (reason=%s, alpaca_id=%s)",
                qty, ticker, limit_price, reason, order_id,
            )

            # Pin order_id → trade_id and transition position state so
            # the next tick doesn't re-fire the same exit. The fill
            # detection in _check_order_statuses will move CLOSING ->
            # CLOSED once the fill confirms.
            if matching_trade_id is not None:
                self._alpaca_to_trade[order_id] = matching_trade_id
                if matching_active is not None:
                    matching_active.status = PositionStatus.CLOSING
                    matching_active.alpaca_exit_order_id = order_id
                    matching_active.exit_reason = reason
                self._update_position_status(
                    matching_trade_id, PositionStatus.CLOSING,
                )
            return order_id
        except Exception:
            logger.exception(
                "Failed to place limit exit for %s (qty=%s, reason=%s) — "
                "rolling back in-memory state",
                ticker, qty, reason,
            )
            # Roll back: there is no Alpaca order, so we must not leave
            # the local state pointing at a phantom CLOSING.
            if matching_active is not None and prior_status is not None:
                matching_active.status = prior_status
                matching_active.alpaca_exit_order_id = None
                matching_active.exit_reason = None
            if order_id is not None:
                self._alpaca_to_trade.pop(order_id, None)
            return None

    async def flatten_all(self) -> None:
        """Flatten all positions with market orders (kill switch)."""
        try:
            await asyncio.to_thread(
                self._gw.client.close_all_positions, cancel_orders=True,
            )
            logger.warning("Flatten all: closed all positions via Alpaca API")
        except Exception:
            logger.exception("Flatten all: error closing positions")

    # ------------------------------------------------------------------
    # State transitions
    # ------------------------------------------------------------------

    async def _transition_to_open(self, trade_id: int, filled_qty: float) -> None:
        """Transition from ENTRY_PENDING to POSITION_OPEN, then attach a stop.

        The entry was submitted as a *plain* LimitOrderRequest (no bracket),
        so we submit a standalone protective stop now. Take-profit is *not*
        sent to Alpaca — the tick-loop's ``check_exits`` polls
        ``target_price`` and exits via ``place_limit_exit``.

        If the stop submission fails, we emergency-flatten and notify, since
        the position would otherwise sit unprotected.
        """
        active: _ActiveOrder | None = self._active_orders.get(trade_id)
        if active is None:
            return

        active.status = PositionStatus.POSITION_OPEN
        active.filled_shares = filled_qty
        self._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        self._update_position_field(trade_id, "quantity", filled_qty)
        self._update_position_field(trade_id, "entry_price", active.entry_price)

        stop_id: str | None = await self._place_standalone_stop(
            trade_id, active, filled_qty,
        )

        if stop_id is None:
            # The submit may have actually reached the broker — alpaca-py
            # response parsing can raise after Alpaca accepted the order.
            # Before emergency-flattening (which is itself fragile and has
            # been observed to silently fail on the same SDK paths), check
            # whether a matching stop is already live at Alpaca and adopt it.
            recovered_stop_id = await self._find_existing_stop(
                active.ticker, filled_qty, stop_price=active.stop_price,
            )
            if recovered_stop_id is not None:
                logger.warning(
                    "Recovered stop attach for %s after submit-response loss: "
                    "alpaca_id=%s (trade_id=%d). Adopting existing broker order.",
                    active.ticker, recovered_stop_id, trade_id,
                )
                stop_id = recovered_stop_id

        if stop_id is None:
            # Stop-attach failure leaves the position unprotected. Recovery:
            # emergency_flatten + notify, then collapse local + DB state to a
            # terminal status so the next stateless cron tick does not
            # re-evaluate a ghost POSITION_OPEN row that has no broker-side
            # stop. Reusing ENTRY_FAILED (rather than CLOSED) is a small
            # semantic stretch — the entry did fill — but it is the only
            # terminal state hydration treats as "do not load," matching the
            # in-memory invariant. A trade_entry notification would be
            # actively misleading for a position that was just force-flattened
            # so we suppress it.
            logger.error(
                "CRITICAL: Failed to attach standalone stop for %s "
                "(trade_id=%d). Emergency flattening.",
                active.ticker, trade_id,
            )
            await self.emergency_flatten(active.ticker, filled_qty, active.exchange)
            await self._notifier.send(
                "Stop Attach Failed",
                f"Could not attach protective stop for {active.ticker} "
                f"(qty={filled_qty:.6f}). Position emergency-flattened. "
                f"Investigate Alpaca order rejections.",
                priority=4,
                tags=["warning"],
            )
            active.status = PositionStatus.ENTRY_FAILED
            self._update_position_status(trade_id, PositionStatus.ENTRY_FAILED)
            self._active_orders.pop(trade_id, None)
            return

        active.alpaca_stop_order_id = stop_id
        self._update_position_field(trade_id, "alpaca_stop_order_id", stop_id)
        # Status name retained for back-compat with downstream readers (DB
        # rows, dashboards, postmortem queries — 45 references across the
        # tree). Under this entry path only the stop is broker-side; take-
        # profit is polled in main.check_exits via target_price. Renaming is
        # tracked as a post-launch follow-up.
        active.status = PositionStatus.STOP_AND_TARGET_ACTIVE
        self._update_position_status(trade_id, PositionStatus.STOP_AND_TARGET_ACTIVE)
        logger.info(
            "Protective stop active for %s (trade_id=%d): stop_id=%s @ %.4f",
            active.ticker, trade_id, stop_id, active.stop_price,
        )

        await self._notifier.trade_entry(
            ticker=active.ticker,
            side="BUY",
            price=active.entry_price,
            qty=filled_qty,
            reason=f"Phase {self._config.get_phase().value} | {active.hold_type}",
        )

    async def _place_standalone_stop(
        self, trade_id: int, active: _ActiveOrder, qty: float,
    ) -> str | None:
        """Submit a standalone sell-stop order. Returns Alpaca order id or None.

        Catches all exceptions so the caller can choose the recovery path
        (emergency flatten + notification) without unwinding the transaction.
        """
        if active.stop_price <= 0:
            logger.error(
                "Refusing to attach stop with non-positive stop_price=%.4f for %s",
                active.stop_price, active.ticker,
            )
            return None
        try:
            request = StopOrderRequest(
                symbol=active.ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=tif_for_stop(qty),
                stop_price=round(active.stop_price, 2),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                self._gw.client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            return str(order.id)
        except Exception:
            logger.exception(
                "Standalone stop submit failed for %s (trade_id=%d, qty=%s, stop=%.4f)",
                active.ticker, trade_id, qty, active.stop_price,
            )
            return None

    async def _find_existing_stop(
        self, ticker: str, qty: float, *, stop_price: float | None = None,
    ) -> str | None:
        """Look up an open SELL stop on ``ticker`` matching ``qty`` at Alpaca.

        Used by ``_transition_to_open`` to recover from a submit-response
        loss: alpaca-py occasionally raises during response parsing after
        the order has actually been accepted by the broker, so the bot
        thinks the stop placement failed when it succeeded. Adopting the
        live order is preferable to emergency-flattening a real position.

        When ``stop_price`` is provided, additionally requires the order's
        stop price to match within 2 cents (Alpaca prices arrive as floats
        or strings; the tolerance covers rounding). This prevents adopting
        an unrelated stop on the same ticker — a real risk under Phase-3
        with eight concurrent positions where a partially-cancelled bracket
        leg and a newly-submitted stop could legitimately coexist briefly.

        Returns the matching order id or None.
        """
        try:
            from alpaca.trading.enums import QueryOrderStatus
            request = GetOrdersRequest(
                status=QueryOrderStatus.OPEN,
                symbols=[ticker],
            )
            orders: list[AlpacaOrder] = await asyncio.to_thread(
                self._gw.client.get_orders,  # type: ignore[arg-type]
                filter=request,
            )
        except Exception:
            logger.warning(
                "Could not query open orders for %s during stop recovery",
                ticker, exc_info=True,
            )
            return None

        for order in orders:
            order_type_obj = getattr(order, "order_type", None) or getattr(
                order, "type", None,
            )
            order_type_str: str = (
                getattr(order_type_obj, "value", "") if order_type_obj is not None else ""
            ).lower()
            side_obj = getattr(order, "side", None)
            side_str: str = (
                getattr(side_obj, "value", "") if side_obj is not None else ""
            ).lower()
            try:
                order_qty: float = float(getattr(order, "qty", 0) or 0)
            except (TypeError, ValueError):
                continue
            if order_type_str != "stop" or side_str != "sell":
                continue
            if abs(order_qty - qty) > 1e-6:
                continue
            if stop_price is not None:
                try:
                    order_stop: float = float(
                        getattr(order, "stop_price", 0) or 0,
                    )
                except (TypeError, ValueError):
                    continue
                if abs(order_stop - stop_price) > 0.02:
                    continue
            return str(order.id)
        return None

    async def activate_trailing_stop(self, trade_id: int, trail_pct: float) -> bool:
        """Activate trailing stop, replacing the take-profit order."""
        active: _ActiveOrder | None = self._active_orders.get(trade_id)
        if active is None:
            return False

        if active.alpaca_target_order_id is not None:
            await self.cancel_order(active.alpaca_target_order_id)
            active.alpaca_target_order_id = None

        trail_id: str | None = await self.place_trailing_stop(
            trade_id, active.ticker, active.exchange,
            active.filled_shares, trail_pct,
        )

        if trail_id is not None:
            active.status = PositionStatus.TRAILING_ACTIVE
            self._update_position_status(trade_id, PositionStatus.TRAILING_ACTIVE)
            logger.info(
                "Trailing stop activated for %s at %.2f%% trail (trade_id=%d)",
                active.ticker, trail_pct * 100, trade_id,
            )
            return True
        return False

    async def check_trail_activations(
        self, get_latest_price: Any,
    ) -> int:
        """Activate trailing stops for positions whose price has crossed the
        configured ``trail_activation_price``. Must be called periodically.

        ``get_latest_price`` is a callable ``(ticker: str) -> float | None``.
        Returns the number of trailing stops activated this call.
        """
        activated: int = 0
        for trade_id, active in list(self._active_orders.items()):
            if active.trail_activated:
                continue
            if active.status != PositionStatus.STOP_AND_TARGET_ACTIVE:
                continue
            if active.trail_pct is None or active.trail_activation_price is None:
                continue

            try:
                price: float | None = get_latest_price(active.ticker)
            except Exception:
                price = None
            if price is None or price <= 0:
                continue

            if price < active.trail_activation_price:
                continue

            logger.info(
                "Trail activation triggered for %s: price=%.2f >= activation=%.2f (trade_id=%d)",
                active.ticker, price, active.trail_activation_price, trade_id,
            )
            ok: bool = await self.activate_trailing_stop(trade_id, active.trail_pct)
            if ok:
                active.trail_activated = True
                activated += 1
        return activated

    async def _cancel_other_exits(
        self, trade_id: int, active: _ActiveOrder, filled_order_id: str,
    ) -> None:
        """Cancel all exit orders except the one that filled."""
        for oid in (
            active.alpaca_stop_order_id,
            active.alpaca_target_order_id,
            active.alpaca_trail_order_id,
        ):
            if oid is not None and oid != filled_order_id:
                await self.cancel_order(oid)

    async def _close_position(
        self, trade_id: int, active: _ActiveOrder,
        exit_price: float, exit_reason: str,
        filled_qty: float | None = None,
    ) -> None:
        """Mark a position as CLOSED in the DB and clean up tracking.

        ``filled_qty`` lets the caller override the share count used for
        P&L when the broker fill diverges from ``active.filled_shares``
        (e.g. a partial-fill exit). Defaults to ``active.filled_shares``
        for callers that don't have a broker-reported quantity in hand.
        """
        now_str: str = datetime.now(tz=ET).isoformat()

        active.status = PositionStatus.CLOSED
        self._update_position_status(trade_id, PositionStatus.CLOSED)

        # Compute realised P&L while we still have the active order in hand.
        # The bot is commission-free so net == gross; FX is 1.0 for USD.
        qty_for_pnl: float = (
            filled_qty if filled_qty is not None and filled_qty > 0
            else active.filled_shares
        )
        gross_pnl: float = (exit_price - active.entry_price) * qty_for_pnl

        # B3: target the trades row by trades.id, not positions.id. The two
        # tables have independent autoincrements; pre-fix this UPDATE
        # silently matched the wrong row (or zero rows).
        db_trade_id: int | None = active.db_trade_id
        if db_trade_id is None:
            logger.warning(
                "_close_position called with no db_trade_id (trade_id=%d "
                "ticker=%s); exit data will be backfilled from Alpaca.",
                trade_id, active.ticker,
            )
        else:
            try:
                conn: sqlite3.Connection = sqlite3.connect(self._db_path)
                try:
                    cur = conn.execute(
                        "UPDATE trades SET exit_time = ?, exit_price = ?, "
                        "exit_reason = ?, gross_pnl = ?, net_pnl = ? "
                        "WHERE id = ?",
                        (
                            now_str,
                            exit_price,
                            exit_reason,
                            gross_pnl,
                            gross_pnl,
                            db_trade_id,
                        ),
                    )
                    conn.commit()
                    if cur.rowcount != 1:
                        logger.warning(
                            "Exit UPDATE on trades.id=%d affected "
                            "rows_affected=%d (expected 1) ticker=%s "
                            "trade_id=%d",
                            db_trade_id, cur.rowcount, active.ticker, trade_id,
                        )
                finally:
                    conn.close()
            except Exception:
                logger.exception(
                    "Failed to update trade record for db_trade_id=%d "
                    "(positions.id=%d)",
                    db_trade_id, trade_id,
                )

        if trade_id in self._active_orders:
            del self._active_orders[trade_id]
        for oid in (
            active.alpaca_entry_order_id,
            active.alpaca_stop_order_id,
            active.alpaca_target_order_id,
            active.alpaca_trail_order_id,
        ):
            if oid is not None:
                self._alpaca_to_trade.pop(oid, None)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _create_position_record(self, decision: EntryDecision) -> int | None:
        now_str: str = datetime.now(tz=ET).isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                # Both inserts share a single transaction. If the trades
                # insert fails we ROLLBACK so we never leave a positions
                # row without its matching trades audit row.
                cursor = conn.execute(
                    "INSERT INTO positions "
                    "(ticker, exchange, currency, sector, quantity, entry_price, "
                    " entry_time, status, stop_price, target_price, hold_type, "
                    " phase, strategy_id, highest_price, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.ticker,
                        decision.exchange,
                        decision.currency,
                        decision.sector,
                        decision.shares,
                        decision.limit_price,
                        now_str,
                        PositionStatus.ENTRY_PENDING.value,
                        decision.stop_price,
                        decision.target_price,
                        decision.hold_type,
                        decision.phase,
                        # Tag the position with the strategy that entered it so
                        # exit logic, per-strategy P&L, and state recovery can
                        # attribute outcomes correctly.
                        decision.strategy_id or "unknown",
                        # Seed highest_price at entry so trailing-stop logic
                        # doesn't see NULL on the first exit evaluation.
                        decision.limit_price,
                        now_str,
                    ),
                )
                trade_id: int = cursor.lastrowid  # type: ignore[assignment]

                # Tag the trades row with strategy_id so postmortems and
                # daily reports can attribute outcomes. Falls back to
                # 'unknown' to mirror the positions row above and keep
                # downstream queries NULL-safe.
                trades_cursor = conn.execute(
                    "INSERT INTO trades "
                    "(ticker, exchange, currency, side, entry_time, entry_price, "
                    " quantity, hold_type, phase, signal_price, sentiment_score, "
                    " signals, strategy_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.ticker,
                        decision.exchange,
                        decision.currency,
                        decision.side,
                        now_str,
                        decision.limit_price,
                        decision.shares,
                        decision.hold_type,
                        decision.phase,
                        decision.limit_price,
                        decision.sentiment_score,
                        decision.signals,
                        decision.strategy_id or "unknown",
                    ),
                )
                # Capture trades.id so the exit-update path can target the
                # correct trades row (B3). trades.id and positions.id are
                # independent autoincrements; pre-fix _close_position used
                # positions.id as the trades WHERE clause.
                db_trade_id: int = trades_cursor.lastrowid  # type: ignore[assignment]
                conn.commit()
                logger.info(
                    "Created position record: %s positions.id=%d trades.id=%d",
                    decision.ticker, trade_id, db_trade_id,
                )
                # Stash on the in-memory tracker so the active order can
                # carry the trades.id forward.
                self._pending_db_trade_ids[trade_id] = db_trade_id
                return trade_id
            finally:
                conn.close()
        except Exception:
            logger.exception("Failed to create position record for %s", decision.ticker)
            return None

    def _update_position_status(self, trade_id: int, status: PositionStatus) -> None:
        self._update_position_field(trade_id, "status", status.value)

    def _mark_trade_entry_failed(self, db_trade_id: int, entry_price: float) -> None:
        """Mark a trades row as entry_failed when Alpaca rejected the submit.

        Closes the row at the original entry price with zero P&L so it is
        excluded from win/loss aggregations but remains visible to the
        reconciler under exit_reason='entry_failed'.
        """
        now_str: str = datetime.now(tz=ET).isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(
                    """
                    UPDATE trades
                       SET exit_time   = ?,
                           exit_price  = ?,
                           exit_reason = 'entry_failed',
                           gross_pnl   = 0,
                           net_pnl     = 0,
                           pnl_usd     = 0
                     WHERE id = ?
                    """,
                    (now_str, entry_price, db_trade_id),
                )
                conn.commit()
                rows_affected: int = cursor.rowcount
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "Failed to mark trade entry_failed for db_trade_id=%d",
                db_trade_id,
            )
            return

        if rows_affected != 1:
            logger.warning(
                "trades UPDATE entry_failed db_trade_id=%d rows_affected=%d "
                "(expected 1) — silent write-back failure",
                db_trade_id, rows_affected,
            )

    # Pre-built UPDATE statements per allowed column. The column name is
    # part of the static SQL; misuse of this helper (e.g. attacker- or
    # config-controlled field name) cannot reach ``conn.execute`` with a
    # column outside this map. Far harder to misuse than a dynamic
    # f-string with a separate allowlist.
    _POSITION_UPDATE_SQL: dict[str, str] = {
        # Column name is part of the static SQL key — comprehension iterates
        # over a hardcoded tuple of column names below. No runtime input
        # reaches the f-string.
        col: f"UPDATE positions SET {col} = ?, updated_at = ? WHERE id = ?"  # nosec B608
        for col in (
            "status", "alpaca_order_id", "alpaca_stop_order_id",
            "alpaca_target_order_id", "alpaca_trail_order_id", "oca_group",
            "quantity", "entry_price", "stop_price", "target_price",
            "trailing_active", "trailing_distance", "highest_price",
        )
    }

    def _update_position_field(self, trade_id: int, field_name: str, value: Any) -> None:
        sql: str | None = self._POSITION_UPDATE_SQL.get(field_name)
        if sql is None:
            logger.error("Attempted to update disallowed field: %s", field_name)
            return

        now_str: str = datetime.now(tz=ET).isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(sql, (value, now_str, trade_id))
                conn.commit()
                rows_affected: int = cursor.rowcount
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "Failed to update positions.%s for trade_id=%d",
                field_name, trade_id,
            )
            return

        # B5 observability: pin which writes succeeded with rows_affected so a
        # silent no-op (wrong WHERE clause, missing row) surfaces immediately.
        # Pre-Phase-3 hypothesis was that this UPDATE was hitting the wrong
        # trade_id; the real cause was upstream submit_order errors / recovery
        # paths skipping the call entirely. Either way, we want the breadcrumb.
        if rows_affected == 1:
            logger.debug(
                "positions UPDATE field=%s trade_id=%d rows_affected=1",
                field_name, trade_id,
            )
        else:
            logger.warning(
                "positions UPDATE field=%s trade_id=%d rows_affected=%d "
                "(expected 1) — silent write-back failure",
                field_name, trade_id, rows_affected,
            )

    def _log_rejection(
        self,
        *,
        ticker: str,
        exchange: str,
        order_type: str,
        intended_price: float | None,
        intended_qty: float | None,
        reason: str,
    ) -> None:
        """Persist an order rejection so post-mortems aren't blind.

        Pre-2026-04-30 the rejection path stamped positions CLOSED but
        wrote nothing to ``order_rejections`` — leaving us with 24 silent
        failures and no forensic trail. This helper closes that gap.
        """
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                repo.save_order_rejection(
                    conn,
                    {
                        "ticker": ticker,
                        "exchange": exchange or "US",
                        "order_type": order_type,
                        "intended_price": intended_price,
                        "intended_qty": intended_qty,
                        "reason": reason,
                        "timestamp": datetime.now(tz=ET).isoformat(),
                        "resolved": 0,
                    },
                )
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "Failed to persist rejection for %s (%s)", ticker, reason
            )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_active_orders(self) -> dict[int, _ActiveOrder]:
        return dict(self._active_orders)

    def get_active_order(self, trade_id: int) -> _ActiveOrder | None:
        return self._active_orders.get(trade_id)

    @property
    def active_count(self) -> int:
        return len(self._active_orders)

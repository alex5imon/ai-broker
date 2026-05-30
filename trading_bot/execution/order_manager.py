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
import contextlib
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


def _coerce_broker_qty(value: Any) -> float | None:
    """Coerce an Alpaca position field to float, or return None.

    Alpaca returns ``qty`` / ``avg_entry_price`` as strings
    ("0.3927", "177.438") via Pydantic-defined ``Optional[str]``
    fields. A defensive coercion: only accept ``str``/``int``/``float``
    so the call site is immune to:

    1. **MagicMock auto-attrs in tests.** ``float(MagicMock())`` returns
       1.0, which would otherwise trick the timeout-fallback consistency
       check in ``_maybe_timeout_entry`` into reporting a held position
       that doesn't exist. The strict isinstance check forces tests to
       set string values explicitly (matching production SDK behavior).
    2. **``bool``-as-``int`` aliasing.** ``isinstance(True, int)`` is
       True in Python, so a stray ``True`` would pass and
       ``float(True) == 1.0`` — wrong for a position quantity. Listing
       the types explicitly and excluding bool via a guard isn't done
       directly here, but the SDK contract guarantees str, not bool, so
       the isinstance check is sufficient — IF the SDK ever changed to
       return bool we'd want to know rather than silently coerce.

    ``decimal.Decimal`` is rejected because alpaca-py's release-pinned
    Pydantic model defines these fields as ``str`` and never produces
    Decimal. If that changes we want a None return + explicit log over
    silent type-coercion that could lose precision.
    """
    if value is None:
        return None
    # `bool` is a subclass of `int`; reject explicitly because
    # `float(True) == 1.0` would silently look like a 1-share position.
    if isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# Callback fired when a strategy-driven exit order (place_exit /
# place_limit_exit) actually FILLS at the broker. Args:
# (strategy_id, ticker, filled_qty, realized_pnl). The pnl is computed
# from the real broker fill price, so the loss-cooldown tracker sees
# slippage-true outcomes rather than signal-time mid-price estimates.
# Bracket-leg exits (stop/target/trail) intentionally do not fire this
# callback today — see item #9 follow-up in risk-infrastructure-gaps.
ExitFillCallback = Callable[[str, str, float, float], None]


def _signed_pnl(
    entry_price: float,
    exit_price: float,
    qty: float,
    side: str,
) -> float:
    """Return realised P&L for a closed position.

    For a long (``side == "BUY"``) profit accrues when ``exit > entry``.
    For a short (``side == "SELL"``) profit accrues when ``exit < entry``
    because the position was sold high and bought back lower.

    All current strategies are long-only so the short branch is currently
    untested by a production code path, but the formula is covered by
    unit tests (``test_order_manager_pnl_sign``) and will be exercised
    automatically once a short-side strategy lands (issue #126).
    """
    if side == "BUY":
        return (exit_price - entry_price) * qty
    # side == "SELL" (short): profit when exit_price < entry_price
    return (entry_price - exit_price) * qty


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
    # tag as 'reconciliation_mismatch' next tick. Persisted to
    # positions.alpaca_exit_order_id (V11+) so a stateless-tick restart
    # rehydrates the pending order and avoids re-submitting the same
    # exit. Pre-V11 this was in-memory only and every overnight_drift
    # exit fell through to the recovery path.
    alpaca_exit_order_id: str | None = None
    exit_reason: str | None = None
    status: PositionStatus = PositionStatus.ENTRY_PENDING
    side: str = "BUY"          # "BUY" (long) or "SELL" (short); mirrors positions.side
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

        The trades-table primary key is resolved via a LEFT JOIN on
        ``(ticker, entry_time, strategy_id)`` so we only read rows for
        non-terminal positions. Pre-V13 this scanned the entire
        ``trades`` table on every tick and built the pair map in Python;
        the JOIN + the V13 ``idx_trades_ticker_entry_time`` index reduce
        wall-time from O(trades) to O(active positions).

        ``MIN(t.id)`` + ``GROUP BY p.id`` deterministically pick the
        lowest-id matching trades row. Production has duplicates on
        ``(ticker, entry_time)`` (recovery path over-inserts stub rows);
        the entry row is always written before any stub, so the lowest
        id is the entry row. Including ``strategy_id`` in the JOIN
        predicate is defense in depth against future cross-strategy
        collisions (issue #128).
        """
        try:
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT p.*, MIN(t.id) AS db_trade_id "
                    "FROM positions p "
                    "LEFT JOIN trades t "
                    "  ON t.ticker = p.ticker "
                    " AND t.entry_time = p.entry_time "
                    " AND t.strategy_id = p.strategy_id "
                    "WHERE p.status NOT IN (?, ?) "
                    "GROUP BY p.id",
                    (
                        PositionStatus.CLOSED.value,
                        PositionStatus.ENTRY_FAILED.value,
                    ),
                ).fetchall()
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

            # alpaca_exit_order_id added in V11. Existing DBs may have
            # this column NULL on every row until a place_exit fires;
            # the `[idx] if "alpaca_exit_order_id" in row.keys()` check
            # tolerates older schema views during the migration window.
            row_keys = set(row.keys())
            exit_oid: str | None = (
                row["alpaca_exit_order_id"]
                if "alpaca_exit_order_id" in row_keys
                else None
            )
            # exit_reason added in V14. Pre-V14 the in-memory
            # ``_ActiveOrder.exit_reason`` was set in ``place_exit``
            # but never persisted, so the fill-detection tick rebuilt
            # the dict from the DB with ``exit_reason=None`` and the
            # fallback ``"strategy_exit"`` won (regression observed
            # 2026-05-18: overnight_drift exits landed as
            # ``strategy_exit`` instead of ``overnight_exit``).
            exit_reason_persisted: str | None = (
                row["exit_reason"]
                if "exit_reason" in row_keys
                else None
            )

            db_trade_id_raw = row["db_trade_id"]
            db_trade_id: int | None = (
                int(db_trade_id_raw) if db_trade_id_raw is not None else None
            )

            active = _ActiveOrder(
                trade_id=trade_id,
                ticker=str(row["ticker"]),
                exchange=str(row["exchange"]),
                alpaca_entry_order_id=row["alpaca_order_id"],
                alpaca_stop_order_id=row["alpaca_stop_order_id"],
                alpaca_target_order_id=row["alpaca_target_order_id"],
                alpaca_trail_order_id=row["alpaca_trail_order_id"],
                alpaca_exit_order_id=exit_oid,
                exit_reason=exit_reason_persisted,
                status=status,
                side=str(row["side"]) if row["side"] else "BUY",
                entry_shares=float(row["quantity"] or 0),
                filled_shares=float(row["quantity"] or 0)
                if status != PositionStatus.ENTRY_PENDING
                else 0.0,
                entry_price=float(row["entry_price"] or 0),
                stop_price=float(row["stop_price"] or 0),
                target_price=float(row["target_price"] or 0),
                hold_type=str(row["hold_type"]),
                strategy_id=row["strategy_id"],
                db_trade_id=db_trade_id,
            )
            self._active_orders[trade_id] = active
            for oid in (
                active.alpaca_entry_order_id,
                active.alpaca_stop_order_id,
                active.alpaca_target_order_id,
                active.alpaca_trail_order_id,
                active.alpaca_exit_order_id,
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
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, ticker, entry_time FROM positions "
                    "WHERE status = ? AND alpaca_order_id IS NULL",
                    (PositionStatus.ENTRY_PENDING.value,),
                ).fetchall()
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
                PositionStatus.STOP_ACTIVE,
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
                            pnl: float = _signed_pnl(
                                active.entry_price, exit_price,
                                active.filled_shares, active.side,
                            )
                            await self._notifier.position_closed(
                                ticker=active.ticker, pnl=pnl,
                                hold_time="", exit_reason=exit_reason.value,
                            )
                            break
                        if order.status.value in ("canceled", "expired", "rejected"):
                            # Issue #117 failure mode A: a stop/target/trail
                            # cancelled at the broker (DAY expiry, external
                            # cancel, EOD wind-down) leaves the row pinned to
                            # STOP_ACTIVE with a dead order_id —
                            # subsequent ticks never re-attach because no
                            # branch handles that state. Clear the dead id
                            # and, for the protective stop specifically,
                            # demote to POSITION_OPEN so the recovery branch
                            # below re-attaches a fresh stop this tick.
                            logger.warning(
                                "Exit order %s for %s is %s — clearing dead id "
                                "(trade_id=%d, leg=%s)",
                                order_id, active.ticker, order.status.value,
                                trade_id, order_id_attr,
                            )
                            setattr(active, order_id_attr, None)
                            self._update_position_field(
                                trade_id, order_id_attr, None,
                            )
                            self._alpaca_to_trade.pop(order_id, None)
                            if order_id_attr == "alpaca_stop_order_id":
                                # Protective stop gone — demote so the
                                # recovery branch below re-attaches.
                                active.status = PositionStatus.POSITION_OPEN
                                self._update_position_status(
                                    trade_id, PositionStatus.POSITION_OPEN,
                                )
                            elif (
                                order_id_attr == "alpaca_trail_order_id"
                                and active.status == PositionStatus.TRAILING_ACTIVE
                            ):
                                # Trail order gone but fixed stop
                                # (alpaca_stop_order_id) is independent —
                                # not nulled when activate_trailing_stop
                                # ran, so the position is still protected.
                                # Demote to STOP_ACTIVE so the
                                # trail re-activation logic in
                                # check_trail_activations can re-fire
                                # next time price crosses the threshold;
                                # otherwise the row sticks in
                                # TRAILING_ACTIVE with a NULL trail id
                                # forever.
                                active.trail_activated = False
                                active.status = (
                                    PositionStatus.STOP_ACTIVE
                                )
                                self._update_position_status(
                                    trade_id,
                                    PositionStatus.STOP_ACTIVE,
                                )
                            # Don't continue checking remaining legs on
                            # this iteration — the row's status has
                            # changed and the next tick will re-evaluate
                            # cleanly.
                            break
                    except Exception:
                        logger.warning(
                            "Error checking exit order for %s", active.ticker,
                            exc_info=True,
                        )

            # Issue #117 failure mode A, POSITION_OPEN variant (2026-05-29):
            # standalone protective stops for mean_reversion / overnight_drift
            # / opening_range_breakout live on a row that stays POSITION_OPEN
            # (they never transition to STOP_ACTIVE). The canceled/expired/
            # rejected handler above only runs for STOP_ACTIVE/TRAILING_ACTIVE,
            # and the re-attach branch below only fires when the id is already
            # None — so a standalone stop that the broker cancels (DAY expiry,
            # external cancel) was healed by NEITHER, leaving the position
            # naked overnight (XLC carried unprotected on 2026-05-29). Poll
            # the recorded stop here; if it's dead, clear the id so the
            # recovery branch below re-attaches a fresh stop this same tick.
            # Two outcomes are handled: a 'filled' stop is a genuine
            # stop_loss exit (attribute it cleanly — pre-fix the
            # POSITION_OPEN poll never checked, so the fill went
            # unattributed and the next state-recovery pass swept it as
            # reconciliation_mismatch with NULL pnl: NVDA/XLF/XLB on
            # 2026-05-29). A canceled/expired/rejected stop is dead — clear
            # the id so the recovery branch below re-attaches a fresh one.
            if (
                active.status == PositionStatus.POSITION_OPEN
                and active.alpaca_stop_order_id is not None
                and active.filled_shares > 0
            ):
                stop_oid: str = active.alpaca_stop_order_id
                try:
                    stop_order: AlpacaOrder = await asyncio.to_thread(
                        client.get_order_by_id,  # type: ignore[arg-type]
                        stop_oid,
                    )
                    stop_status: str = (
                        getattr(stop_order.status, "value", "") or ""
                    ).lower()
                    if stop_status == "filled":
                        stop_exit_price: float = float(
                            stop_order.filled_avg_price or 0
                        )
                        logger.info(
                            "Standalone stop FILLED: %s @ %.4f reason=%s "
                            "(trade_id=%d)",
                            active.ticker, stop_exit_price,
                            ExitReason.STOP_LOSS.value, trade_id,
                        )
                        await self._cancel_other_exits(
                            trade_id, active, stop_oid,
                        )
                        await self._close_position(
                            trade_id, active, stop_exit_price,
                            ExitReason.STOP_LOSS.value,
                        )
                        stop_pnl: float = _signed_pnl(
                            active.entry_price, stop_exit_price,
                            active.filled_shares, active.side,
                        )
                        await self._notifier.position_closed(
                            ticker=active.ticker, pnl=stop_pnl,
                            hold_time="",
                            exit_reason=ExitReason.STOP_LOSS.value,
                        )
                    elif stop_status in ("canceled", "expired", "rejected"):
                        logger.warning(
                            "Standalone stop %s for %s is %s while position is "
                            "POSITION_OPEN — clearing dead id so it re-attaches "
                            "this tick (trade_id=%d)",
                            stop_oid, active.ticker, stop_status, trade_id,
                        )
                        self._alpaca_to_trade.pop(stop_oid, None)
                        active.alpaca_stop_order_id = None
                        self._update_position_field(
                            trade_id, "alpaca_stop_order_id", None,
                        )
                except Exception:
                    logger.warning(
                        "Error checking standalone stop %s for %s "
                        "(trade_id=%d)",
                        stop_oid, active.ticker, trade_id, exc_info=True,
                    )

            # Issue #117 failure modes B & C: a position can land in
            # POSITION_OPEN with alpaca_stop_order_id=NULL when
            # _transition_to_open was interrupted between the status flip
            # (line 1281) and the stop-id write (line 1337) — a killed
            # cron tick, an SDK hang, or a silent submit-response loss.
            # Pre-fix the only thing that healed this was the next-market-
            # open _verify_stop_orders sweep, leaving the position naked
            # for up to a full overnight + weekend window. Re-attempt the
            # stop attach during market hours so the protective stop is
            # back on the book within one tick (≤ 5 min) of the gap.
            if (
                active.status == PositionStatus.POSITION_OPEN
                and active.alpaca_stop_order_id is None
                and active.filled_shares > 0
            ):
                if active.stop_price <= 0:
                    # Defence-in-depth: a row with status=POSITION_OPEN
                    # but no stop_price means the strategy or persistence
                    # layer dropped it. _place_standalone_stop refuses
                    # to attach a non-positive stop anyway; surface the
                    # state explicitly rather than silently skip so the
                    # operator can spot the upstream bug.
                    logger.warning(
                        "Stop recovery skipped for %s (trade_id=%d): "
                        "stop_price=%.4f is non-positive — cannot attach. "
                        "Inspect upstream strategy/persistence path.",
                        active.ticker, trade_id, active.stop_price,
                    )
                else:
                    await self._recover_missing_stop(trade_id, active)

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
                        pnl_strategy: float = _signed_pnl(
                            active.entry_price, exit_px,
                            filled_exit_qty, active.side,
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
                        # the limit). Roll back to STOP_ACTIVE so
                        # the next tick re-evaluates the exit condition; the
                        # protective stop is re-attached by recovery if it
                        # was cancelled.
                        logger.warning(
                            "Strategy exit %s for %s — rolling back to STOP_ACTIVE",
                            exit_order.status.value, active.ticker,  # type: ignore[union-attr]
                        )
                        active.alpaca_exit_order_id = None
                        active.exit_reason = None
                        active.status = PositionStatus.STOP_ACTIVE
                        # V11+: clear the persisted exit order_id too,
                        # so the next tick re-evaluates the exit
                        # condition cleanly instead of seeing a stale
                        # alpaca_exit_order_id and refusing to act.
                        # V14+: clear the persisted ``exit_reason`` in
                        # the same step or the next ``place_exit`` would
                        # inherit the old reason if it raced ahead of
                        # the new assignment.
                        self._update_position_field(
                            trade_id, "alpaca_exit_order_id", None,
                        )
                        self._update_position_field(
                            trade_id, "exit_reason", None,
                        )
                        self._update_position_status(
                            trade_id, PositionStatus.STOP_ACTIVE,
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
                side=decision.side,
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
            # Belt-and-suspenders: before declaring ENTRY_FAILED, ask the
            # broker directly whether we actually hold the position. The
            # order-status endpoint can lag the fill by seconds (observed
            # 2026-05-11: 169 ms fill, 5-min poll still showed
            # filled_qty=0), which would otherwise orphan a real
            # position. If the position exists at the broker, treat the
            # order as filled and transition to POSITION_OPEN with the
            # actual broker qty + avg cost.
            broker_qty: float | None = await self._broker_held_qty(active.ticker)
            if broker_qty is not None and broker_qty > 0:
                broker_avg: float = await self._broker_avg_entry_price(
                    active.ticker,
                ) or active.entry_price
                logger.warning(
                    "Entry timeout: order-status lag for %s — broker holds "
                    "%.6f sh @ $%.4f despite filled_qty=0 on order. "
                    "Promoting to POSITION_OPEN (trade_id=%d, age=%.0fs).",
                    active.ticker, broker_qty, broker_avg, trade_id,
                    age.total_seconds(),
                )
                active.filled_shares = broker_qty
                active.entry_price = broker_avg
                await self._transition_to_open(trade_id, broker_qty)
                return

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

    async def _broker_held_qty(self, ticker: str) -> float | None:
        """Return Alpaca-side held qty for ``ticker``, or None on lookup
        failure / no position. Used by ``_maybe_timeout_entry`` to avoid
        marking a position ENTRY_FAILED when the broker has actually
        filled the order but order-status hasn't propagated yet.
        """
        try:
            pos = await asyncio.to_thread(
                self._gw.client.get_open_position, ticker,  # type: ignore[arg-type]
            )
        except Exception:
            # No open position OR transient error — fall through to the
            # legacy ENTRY_FAILED path. Don't try to distinguish; the
            # consistency check is best-effort and should never break
            # the timeout path.
            return None
        return _coerce_broker_qty(getattr(pos, "qty", None))

    async def _broker_avg_entry_price(self, ticker: str) -> float | None:
        """Mirror of _broker_held_qty for the broker's avg entry price."""
        try:
            pos = await asyncio.to_thread(
                self._gw.client.get_open_position, ticker,  # type: ignore[arg-type]
            )
        except Exception:
            return None
        return _coerce_broker_qty(getattr(pos, "avg_entry_price", None))

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

    async def cancel_all_for_ticker(
        self, ticker: str, *, side_filter: OrderSide | None = None,
    ) -> None:
        """Cancel open orders for a ticker.

        Args:
            ticker: Symbol to scope the cancel to.
            side_filter: If set, only cancel orders on this side. Used by
                strategy-exit paths to pass ``OrderSide.SELL`` so an
                in-flight BUY entry from another strategy on the same
                ticker isn't collaterally cancelled. Default ``None``
                preserves the pre-existing all-orders behaviour for
                emergency-flatten / drain paths.
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
            cancelled: int = 0
            filter_value: str | None = (
                side_filter.value if side_filter is not None else None
            )
            for order in orders:
                if filter_value is not None:
                    # alpaca-py's Order.side is Optional[OrderSide]
                    # (Pydantic enum). We also tolerate a raw-string
                    # ``side`` in case the SDK ever flattens enums in
                    # a future release. None → can't classify, skip
                    # safely rather than nuking an order whose side
                    # we don't know.
                    order_side: OrderSide | str | None = getattr(
                        order, "side", None,
                    )
                    if order_side is None:
                        continue
                    order_side_value: str
                    if isinstance(order_side, str):
                        order_side_value = order_side
                    else:
                        # OrderSide.value is `str` per alpaca-py's
                        # Pydantic model — the isinstance guard is
                        # defensive against a future SDK shape change.
                        side_value_attr = getattr(order_side, "value", None)
                        if not isinstance(side_value_attr, str):
                            continue
                        order_side_value = side_value_attr
                    if order_side_value != filter_value:
                        continue
                try:
                    await asyncio.to_thread(
                        self._gw.client.cancel_order_by_id, str(order.id),
                    )
                    cancelled += 1
                except Exception:
                    logger.warning("Error cancelling order for %s", ticker, exc_info=True)
            if cancelled:
                logger.info(
                    "Cancelled %d order(s) for %s%s",
                    cancelled, ticker,
                    f" (side={filter_value})" if filter_value else "",
                )
        except Exception:
            logger.exception("Error cancelling orders for %s", ticker)

    async def emergency_flatten(
        self, ticker: str, qty: float, exchange: str,
        *, position_side: str = "BUY",
    ) -> None:
        """Emergency market flatten.

        Flattens a long (``position_side == "BUY"``) by SELLing and a short
        (``position_side == "SELL"``) by BUYing to cover. ``position_side``
        defaults to BUY so existing callers are unchanged; callers that may
        hold shorts (wind-down, the legacy exit path, ``_transition_to_open``)
        pass the position's side. Flattening a short with the default SELL
        would *double* the short — hence the explicit threading.
        """
        flatten_side: OrderSide = (
            OrderSide.BUY if position_side == "SELL" else OrderSide.SELL
        )
        client: TradingClient = self._gw.client
        try:
            request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=flatten_side,
                type=OrderType.MARKET,
                time_in_force=tif_for_market(qty),
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            logger.warning(
                "Emergency flatten: %s %.6f %s @ MARKET (alpaca_id=%s)",
                flatten_side.value, qty, ticker, str(order.id),
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
        #
        # Direction-aware cover + cancel. Long: SELL to close, cancel the
        # protective SELL stop (preserve any in-flight BUY entry from
        # another sleeve). Short: BUY to cover, cancel the protective BUY
        # stop (preserve any in-flight SELL entry). The cancel releases the
        # held_for_orders qty that would otherwise reject the exit with
        # code 40310000 ("insufficient qty available"). Falls back to long
        # when there's no matching in-memory order (recovery/drain).
        is_short: bool = (
            matching_active is not None and matching_active.side == "SELL"
        )
        exit_side: OrderSide = OrderSide.BUY if is_short else OrderSide.SELL
        protective_side: OrderSide = OrderSide.BUY if is_short else OrderSide.SELL
        await self.cancel_all_for_ticker(ticker, side_filter=protective_side)

        client: TradingClient = self._gw.client
        try:
            request = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=exit_side,
                type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
            )
            order: AlpacaOrder = await asyncio.to_thread(
                client.submit_order,  # type: ignore[arg-type]
                order_data=request,
            )
            order_id: str = str(order.id)
            logger.info(
                "Strategy exit submitted: %s %s %s @ MARKET (reason=%s, alpaca_id=%s)",
                exit_side.value, qty, ticker, reason, order_id,
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
                # Persist the exit order_id BEFORE flipping status to
                # CLOSING — a crash between the two writes leaves the
                # row still STOP_ACTIVE with the order id
                # set, which the next tick's rehydration handles
                # gracefully. V11+: column lives on positions table.
                # Persist ``exit_reason`` in the same step (V14+) so the
                # fill-detection tick can rehydrate it instead of
                # falling back to ``"strategy_exit"``.
                self._update_position_field(
                    matching_trade_id, "alpaca_exit_order_id", order_id,
                )
                self._update_position_field(
                    matching_trade_id, "exit_reason", reason,
                )
                self._update_position_status(
                    matching_trade_id, PositionStatus.CLOSING,
                )
            return order_id
        except Exception as exc:
            logger.exception(
                "CRITICAL: Failed to place strategy exit for %s (qty=%s, reason=%s)",
                ticker, qty, reason,
            )
            # Always notify — every place_exit caller intends to actually
            # close a real position, so a failure means the position is
            # stuck. Today's silent failure mode (2026-05-08 → 2026-05-11
            # daily "insufficient qty" rejections, no alert ever fired)
            # showed that the prior is_emergency-only path was too
            # restrictive. is_emergency now only escalates priority.
            priority: int = 5 if is_emergency else 4
            await self._notifier.send(
                "Strategy Exit Failed",
                (
                    f"Failed to exit {qty} {ticker} (reason={reason}). "
                    f"Position remains long at the broker. Manual review "
                    f"required. Error: {type(exc).__name__}: {exc}"
                ),
                priority=priority,
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

        # Direction-aware cover + cancel — mirrors place_exit. Long: SELL
        # to close, cancel the protective SELL stop. Short: BUY to cover,
        # cancel the protective BUY stop. The opposite side may be a
        # concurrent sleeve's in-flight entry, so we never cancel it.
        is_short: bool = (
            matching_active is not None and matching_active.side == "SELL"
        )
        exit_side: OrderSide = OrderSide.BUY if is_short else OrderSide.SELL
        protective_side: OrderSide = OrderSide.BUY if is_short else OrderSide.SELL
        await self.cancel_all_for_ticker(ticker, side_filter=protective_side)

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
                side=exit_side,
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
                "Limit exit submitted: %s %s %s @ %.2f (reason=%s, alpaca_id=%s)",
                exit_side.value, qty, ticker, limit_price, reason, order_id,
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
                # V11+: persist exit order_id before status flip so a
                # crash between the two writes is recoverable from DB.
                # V14+: persist ``exit_reason`` in the same step so the
                # fill-detection tick uses the real reason instead of
                # the ``"strategy_exit"`` fallback.
                self._update_position_field(
                    matching_trade_id, "alpaca_exit_order_id", order_id,
                )
                self._update_position_field(
                    matching_trade_id, "exit_reason", reason,
                )
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
                position_side=active.side,
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
            await self.emergency_flatten(
                active.ticker, filled_qty, active.exchange,
                position_side=active.side,
            )
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
        active.status = PositionStatus.STOP_ACTIVE
        self._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
        logger.info(
            "Protective stop active for %s (trade_id=%d): stop_id=%s @ %.4f",
            active.ticker, trade_id, stop_id, active.stop_price,
        )

        await self._notifier.trade_entry(
            ticker=active.ticker,
            side=active.side,
            price=active.entry_price,
            qty=filled_qty,
            reason=f"Phase {self._config.get_phase().value} | {active.hold_type}",
        )

    async def _place_standalone_stop(
        self, trade_id: int, active: _ActiveOrder, qty: float,
    ) -> str | None:
        """Submit a standalone protective stop. Returns Alpaca order id or None.

        A long (``side == "BUY"``) is protected by a SELL stop below entry;
        a short (``side == "SELL"``) by a BUY stop above entry. The strategy
        supplies a stop_price already on the correct side of entry — here we
        only flip the order side.

        Catches all exceptions so the caller can choose the recovery path
        (emergency flatten + notification) without unwinding the transaction.
        """
        if active.stop_price <= 0:
            logger.error(
                "Refusing to attach stop with non-positive stop_price=%.4f for %s",
                active.stop_price, active.ticker,
            )
            return None
        stop_side: OrderSide = (
            OrderSide.BUY if active.side == "SELL" else OrderSide.SELL
        )
        try:
            request = StopOrderRequest(
                symbol=active.ticker,
                qty=qty,
                side=stop_side,
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
        position_side: str = "BUY",
    ) -> str | None:
        """Look up an open protective stop on ``ticker`` matching ``qty``.

        The protective side depends on the position: a long (``side ==
        "BUY"``) is protected by a SELL stop, a short by a BUY stop.
        Defaults to the long case.

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
            # A short's protective stop is a BUY; a long's a SELL.
            expected_side: str = "buy" if position_side == "SELL" else "sell"
            if order_type_str != "stop" or side_str != expected_side:
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

    # Minimum seconds before close required for a DAY stop to make it onto
    # the book before Alpaca defers it to the next session. Mirrors
    # recovery._STOP_VERIFY_CLOSE_BUFFER_SEC (PR #102) — the same phantom-
    # stop bug applies here: a stop submitted at 15:57 ET is deferred to
    # the next session's pre-market open where it holds the qty and
    # blocks the morning's strategy exit.
    _STOP_RECOVER_CLOSE_BUFFER_SEC: int = 300

    async def _inside_market_hours_for_stop_attach(self) -> bool:
        """Return True when the recovery branch may submit a fresh stop.

        Primary: consult Alpaca's clock (`is_open` + `next_close`) so
        early-close days (Thanksgiving Friday, July 3, Christmas Eve)
        are handled correctly — a fixed 15:55 ET cutoff would happily
        place a stop at 13:30 on an early-close day, which Alpaca would
        defer to the next session.

        Fallback: fixed time gate 09:30 ≤ now_et < 15:55 ET.
        """
        try:
            clock = await asyncio.to_thread(self._gw.client.get_clock)
            is_open = getattr(clock, "is_open", None)
            if isinstance(is_open, bool):
                if not is_open:
                    return False
                next_close = getattr(clock, "next_close", None)
                if isinstance(next_close, datetime):
                    if next_close.tzinfo is None:
                        next_close = next_close.replace(tzinfo=ET)
                    seconds_to_close: float = (
                        next_close - datetime.now(tz=ET)
                    ).total_seconds()
                    if seconds_to_close < self._STOP_RECOVER_CLOSE_BUFFER_SEC:
                        return False
                return True
        except Exception:
            logger.debug(
                "AlpacaClock unavailable for stop-recover gate; "
                "falling back to fixed time window",
                exc_info=True,
            )
        now_et: datetime = datetime.now(tz=ET)
        hhmm: int = now_et.hour * 60 + now_et.minute
        return 9 * 60 + 30 <= hhmm < 15 * 60 + 55

    async def _recover_missing_stop(
        self, trade_id: int, active: _ActiveOrder,
    ) -> None:
        """Attach a standalone stop on a POSITION_OPEN row that has none.

        Called from ``_check_order_statuses`` when a row is rehydrated
        with ``status=POSITION_OPEN`` and ``alpaca_stop_order_id=NULL``
        — the survivor of a crashed/interrupted ``_transition_to_open``
        (issue #117 failure modes B & C) or of a stop that was cancelled
        at the broker without re-attachment (failure mode A, after the
        cancellation handler in ``_check_order_statuses`` demotes the
        row's status). Gated on market hours to avoid the phantom-stop
        bug that PR #102 fixed for ``recovery._verify_stop_orders``.

        On submit-response loss (alpaca-py occasionally raises after
        Alpaca accepted the order) we adopt any matching open stop via
        ``_find_existing_stop`` rather than emergency-flattening a real
        position. On total failure the row stays POSITION_OPEN so the
        next tick retries — and ``recovery._verify_stop_orders`` is the
        ultimate fallback at the next market open.
        """
        if not await self._inside_market_hours_for_stop_attach():
            logger.info(
                "Stop recovery deferred for %s (trade_id=%d) — outside "
                "safe placement window; next market-open recovery sweep "
                "will heal.",
                active.ticker, trade_id,
            )
            return

        recovered: str | None = await self._find_existing_stop(
            active.ticker, active.filled_shares, stop_price=active.stop_price,
        )
        if recovered is not None:
            logger.warning(
                "Stop recovery: adopting existing broker stop for %s "
                "(trade_id=%d, alpaca_id=%s) — DB row was POSITION_OPEN "
                "with no recorded stop_order_id.",
                active.ticker, trade_id, recovered,
            )
            active.alpaca_stop_order_id = recovered
            self._alpaca_to_trade[recovered] = trade_id
            self._update_position_field(
                trade_id, "alpaca_stop_order_id", recovered,
            )
            active.status = PositionStatus.STOP_ACTIVE
            self._update_position_status(
                trade_id, PositionStatus.STOP_ACTIVE,
            )
            await self._notifier.send(
                "Stop Recovery: Adopted Existing Broker Stop",
                (
                    f"Adopted broker-side stop for {active.ticker} "
                    f"(trade_id={trade_id}, alpaca_id={recovered}). "
                    f"Position was POSITION_OPEN without a recorded "
                    f"stop_order_id; investigate issue #117 lineage."
                ),
                priority=3,
                tags=["warning"],
            )
            return

        stop_id: str | None = await self._place_standalone_stop(
            trade_id, active, active.filled_shares,
        )
        if stop_id is None:
            logger.warning(
                "Stop recovery: submit failed for %s (trade_id=%d, "
                "qty=%.6f, stop=%.4f) — leaving POSITION_OPEN for next "
                "tick retry; recovery._verify_stop_orders is the final "
                "fallback at next market open.",
                active.ticker, trade_id,
                active.filled_shares, active.stop_price,
            )
            return

        # Persist the order_id BEFORE the status flip so a crash between
        # the two writes leaves the row POSITION_OPEN + stop_id set —
        # the next tick's recovery treats that as already-healed.
        active.alpaca_stop_order_id = stop_id
        self._alpaca_to_trade[stop_id] = trade_id
        self._update_position_field(trade_id, "alpaca_stop_order_id", stop_id)
        active.status = PositionStatus.STOP_ACTIVE
        self._update_position_status(
            trade_id, PositionStatus.STOP_ACTIVE,
        )
        logger.warning(
            "Stop recovery: attached fresh stop %s for %s (trade_id=%d, "
            "qty=%.6f, stop=%.4f) — position was naked since prior tick.",
            stop_id, active.ticker, trade_id,
            active.filled_shares, active.stop_price,
        )
        await self._notifier.send(
            "Stop Recovery: Re-attached Protective Stop",
            (
                f"Re-attached protective stop for {active.ticker} "
                f"(trade_id={trade_id}, qty={active.filled_shares:.6f}, "
                f"stop=${active.stop_price:.4f}, alpaca_id={stop_id}). "
                f"Position was POSITION_OPEN without a stop; this is "
                f"the issue #117 failure surface — investigate prior "
                f"tick's logs."
            ),
            priority=3,
            tags=["warning"],
        )

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
            if active.status != PositionStatus.STOP_ACTIVE:
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
        gross_pnl: float = _signed_pnl(
            active.entry_price, exit_price, qty_for_pnl, active.side,
        )

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
                with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
                    # ``pnl_usd`` must be written in lockstep with
                    # ``net_pnl`` — downstream readers
                    # (``performance.calculate_daily_metrics``,
                    # ``repo.get_daily_pnl_usd``, the daily-loss
                    # circuit breaker, ``_save_daily_summary``) key off
                    # ``pnl_usd`` and silently treat NULL as zero,
                    # which classified every live exit as neither win
                    # nor loss and zeroed the daily-loss circuit's view
                    # of realised P&L. The bot is commission-free so
                    # gross/net/pnl_usd are equal at close time.
                    cur = conn.execute(
                        "UPDATE trades SET exit_time = ?, exit_price = ?, "
                        "exit_reason = ?, gross_pnl = ?, net_pnl = ?, "
                        "pnl_usd = ? "
                        "WHERE id = ?",
                        (
                            now_str,
                            exit_price,
                            exit_reason,
                            gross_pnl,
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
            active.alpaca_exit_order_id,
        ):
            if oid is not None:
                self._alpaca_to_trade.pop(oid, None)

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _create_position_record(self, decision: EntryDecision) -> int | None:
        now_str: str = datetime.now(tz=ET).isoformat()
        try:
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
                # Both inserts share a single transaction. If the trades
                # insert fails we ROLLBACK so we never leave a positions
                # row without its matching trades audit row.
                cursor = conn.execute(
                    "INSERT INTO positions "
                    "(ticker, exchange, currency, sector, side, quantity, entry_price, "
                    " entry_time, status, stop_price, target_price, hold_type, "
                    " phase, strategy_id, highest_price, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        decision.ticker,
                        decision.exchange,
                        decision.currency,
                        decision.sector,
                        decision.side,
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
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
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
            "alpaca_target_order_id", "alpaca_trail_order_id",
            "alpaca_exit_order_id", "exit_reason", "oca_group",
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
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
                cursor = conn.execute(sql, (value, now_str, trade_id))
                conn.commit()
                rows_affected: int = cursor.rowcount
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
            with contextlib.closing(sqlite3.connect(self._db_path)) as conn:
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

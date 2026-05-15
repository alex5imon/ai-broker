"""State recovery and reconciliation on startup.

Alpaca is the source of truth.  On every startup the bot queries Alpaca
for positions, orders, and account state, then reconciles with the local
SQLite database before resuming normal operation.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import Order as AlpacaOrder

from trading_bot.constants import TZ_EASTERN
from trading_bot.db.repository import close_or_insert_trade_row
from trading_bot.gateway.connection import GatewayConnection
from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN

# Default thresholds — overridable via config.
_DEFAULT_STALE_ENTRY_MINUTES: int = 5
_DEFAULT_EOD_FLATTEN_TIME: str = "15:55"
# Don't close DB-only positions whose entry_time is younger than this.
# Alpaca's positions endpoint can lag a fresh fill by several minutes,
# so a brand-new entry can briefly look DB-only and trip the mismatch
# branch. Override via config.recovery.fresh_entry_grace_minutes.
_DEFAULT_FRESH_ENTRY_GRACE_MINUTES: int = 10

# Position statuses that indicate an exit order is already on the
# broker book. When Alpaca shows no position for a ticker in one of
# these states AND the corresponding alpaca_*_order_id is set, defer
# the close to `_check_order_statuses` — it will poll the order_id,
# detect the fill, and write the real exit_price/exit_reason/pnl onto
# the trades row. Without this defer, recovery races
# `_check_order_statuses` on the same tick and clobbers the proper
# exit attribution with 'reconciliation_mismatch' + NULL pnl
# (regression observed 2026-05-15 — see PR #140).
_EXIT_IN_FLIGHT_STATUSES: frozenset[str] = frozenset(
    {"CLOSING", "STOP_ACTIVE", "TRAILING_ACTIVE"}
)
_EXIT_ORDER_ID_COLUMNS: tuple[str, ...] = (
    "alpaca_exit_order_id",
    "alpaca_stop_order_id",
    "alpaca_trail_order_id",
    "alpaca_target_order_id",
)


@dataclass
class RecoveryResult:
    """Summary of the startup state-recovery process."""

    broker_positions: int = 0
    broker_open_orders: int = 0
    db_open_positions: int = 0

    positions_created: list[str] = field(default_factory=list)
    positions_closed_mismatch: list[str] = field(default_factory=list)
    positions_deferred_fresh: list[str] = field(default_factory=list)
    # Rows whose status indicates an exit order is already on the broker
    # book (CLOSING, STOP_ACTIVE, TRAILING_ACTIVE) and that order_id is
    # non-NULL. Deferring lets the next tick's `_check_order_statuses`
    # poll the order and write the real exit_price/exit_reason/pnl
    # rather than recovery clobbering the trades row with
    # 'reconciliation_mismatch' + NULL pnl.
    positions_deferred_exit_inflight: list[str] = field(default_factory=list)
    quantities_updated: list[str] = field(default_factory=list)
    stops_placed: list[str] = field(default_factory=list)
    stale_orders_cancelled: list[str] = field(default_factory=list)
    eod_flatten_orders: list[str] = field(default_factory=list)
    # Pairs of orphan→canonical position IDs merged by
    # _heal_unknown_orphans. Format: "TICKER #orphan→#canonical (strategy)".
    orphans_healed: list[str] = field(default_factory=list)

    account_equity: float = 0.0
    settled_cash: float = 0.0
    buying_power: float = 0.0

    @property
    def has_discrepancies(self) -> bool:
        return bool(
            self.positions_created
            or self.positions_closed_mismatch
            or self.quantities_updated
            or self.stale_orders_cancelled
            or self.eod_flatten_orders
            or self.orphans_healed
        )

    def summary(self) -> str:
        lines: list[str] = [
            f"Alpaca positions: {self.broker_positions}, "
            f"DB open positions: {self.db_open_positions}",
            f"Equity: {self.account_equity:.2f}, "
            f"Settled cash: {self.settled_cash:.2f}, "
            f"Buying power: {self.buying_power:.2f}",
        ]
        if self.positions_created:
            lines.append(f"Created DB records for broker-only positions: {', '.join(self.positions_created)}")
        if self.positions_closed_mismatch:
            lines.append(f"Closed DB-only positions (mismatch): {', '.join(self.positions_closed_mismatch)}")
        if self.positions_deferred_fresh:
            lines.append(
                f"Deferred fresh DB-only positions (Alpaca lag): "
                f"{', '.join(self.positions_deferred_fresh)}"
            )
        if self.positions_deferred_exit_inflight:
            lines.append(
                f"Deferred DB-only positions (exit in flight): "
                f"{', '.join(self.positions_deferred_exit_inflight)}"
            )
        if self.quantities_updated:
            lines.append(f"Updated quantities: {', '.join(self.quantities_updated)}")
        if self.stops_placed:
            lines.append(f"Placed missing stop orders: {', '.join(self.stops_placed)}")
        if self.stale_orders_cancelled:
            lines.append(
                f"Cancelled stale entry orders: {', '.join(self.stale_orders_cancelled)}"
            )
        if self.eod_flatten_orders:
            lines.append(
                f"EOD flatten — closing intraday positions: "
                f"{', '.join(self.eod_flatten_orders)}"
            )
        if self.orphans_healed:
            lines.append(
                f"Healed orphan attribution: {', '.join(self.orphans_healed)}"
            )
        if not self.has_discrepancies:
            lines.append("No discrepancies found - state is clean.")
        return "\n".join(lines)


class StateRecovery:
    """Reconciles Alpaca state with local SQLite state on startup."""

    def __init__(
        self,
        gateway: GatewayConnection,
        db_path: str,
        notifier: Notifier,
        config: dict[str, Any] | None = None,
        now_fn: "Callable[[], datetime] | None" = None,  # noqa: F821
    ) -> None:
        self._gw: GatewayConnection = gateway
        self._db_path: str = db_path
        self._notifier: Notifier = notifier
        self._config: dict[str, Any] = config or {}
        # Injectable clock — defaults to wall clock in ET. Tests pass a
        # fixed clock to exercise the EOD-flatten branches deterministically.
        self._now_fn = now_fn or (lambda: datetime.now(tz=ET))

    async def recover(self) -> RecoveryResult:
        """Run the full state-recovery sequence."""
        result: RecoveryResult = RecoveryResult()

        # 1. Alpaca positions
        alpaca_positions: list[AlpacaPosition] = await self._gw.get_positions()
        result.broker_positions = len(alpaca_positions)

        # 2. Alpaca open orders
        alpaca_orders: list[AlpacaOrder] = await self._gw.get_open_orders()
        result.broker_open_orders = len(alpaca_orders)

        # 3. Account summary
        account: dict[str, Any] = await self._gw.get_account_summary()
        result.account_equity = _safe_float(account.get("NetLiquidation", "0"))
        result.settled_cash = _safe_float(account.get("SettledCash", "0"))
        result.buying_power = _safe_float(account.get("BuyingPower", "0"))

        # 4. Heal stale orphan attribution. If a previous tick captured
        #    an Alpaca position as a strategy_id='unknown' orphan AND a
        #    matching ENTRY_FAILED row exists, merge them so the
        #    strategy's exit logic sees the position. See
        #    _heal_unknown_orphans for the match rules.
        self._heal_unknown_orphans(result)

        # 5. Load SQLite open positions (post-healing so the reconciler
        #    sees the corrected attribution).
        db_positions: list[dict[str, Any]] = self._load_db_open_positions()
        result.db_open_positions = len(db_positions)

        # 6. Reconcile
        self._reconcile(alpaca_positions, db_positions, result)

        # 7. Verify stop orders
        await self._verify_stop_orders(alpaca_positions, alpaca_orders, result)

        # 8. Cancel stale entry orders. Fresh order list because step 7 may
        #    have placed new stops, but those are GTC and not stale.
        await self._cancel_stale_entry_orders(alpaca_orders, result)

        # 9. EOD intraday flatten. Closes intraday-tagged DB positions if the
        #    cron tick lands after the configured cutoff (default 15:55 ET).
        #    Pass open orders for idempotency (skip tickers with a pending
        #    SELL already on the broker).
        await self._eod_intraday_flatten(alpaca_positions, alpaca_orders, result)

        # 10. Log and alert
        logger.info("State recovery complete:\n%s", result.summary())
        if result.has_discrepancies:
            await self._notifier.gateway_alert(
                f"State recovery found discrepancies:\n{result.summary()}",
                is_critical=False,
            )

        return result

    def _load_db_open_positions(self) -> list[dict[str, Any]]:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT * FROM positions WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED')"
            )
            rows: list[dict[str, Any]] = [dict(row) for row in cursor.fetchall()]
            logger.debug("Loaded %d open positions from DB", len(rows))
            return rows
        except sqlite3.OperationalError:
            logger.warning("positions table not found - treating as empty")
            return []
        finally:
            conn.close()

    def _reconcile(
        self,
        alpaca_positions: list[AlpacaPosition],
        db_positions: list[dict[str, Any]],
        result: RecoveryResult,
    ) -> None:
        """Match Alpaca positions against DB records."""
        db_by_ticker: dict[str, dict[str, Any]] = {}
        for row in db_positions:
            db_by_ticker[row["ticker"]] = row

        alpaca_by_ticker: dict[str, AlpacaPosition] = {}
        for pos in alpaca_positions:
            ticker: str = str(pos.symbol)
            # Float qty: a fractional Alpaca position would truncate to 0
            # under int() and get filtered out, hiding a real holding from
            # the reconciler.
            qty: float = float(pos.qty or 0)
            if abs(qty) > 1e-6:
                alpaca_by_ticker[ticker] = pos

        for ticker, pos in alpaca_by_ticker.items():
            if ticker not in db_by_ticker:
                logger.warning(
                    "Alpaca has position %s (qty=%s) not in DB - creating record",
                    ticker, pos.qty,
                )
                self._create_db_position(ticker, pos)
                result.positions_created.append(ticker)
            else:
                db_row: dict[str, Any] = db_by_ticker[ticker]
                # Compare as floats — Alpaca holds fractional quantities (1/1e6
                # increments). int() truncation here would silently mask a
                # 0.43-vs-0 sync gap by collapsing both sides to 0. Use a
                # tolerance to absorb float-repr noise without flagging a
                # legitimate match as a drift.
                alpaca_qty: float = float(pos.qty or 0)
                db_qty: float = float(db_row.get("quantity", 0))
                if abs(alpaca_qty - db_qty) > 1e-6:
                    logger.warning(
                        "Quantity mismatch for %s: Alpaca=%.6f, DB=%.6f. Updating DB.",
                        ticker, alpaca_qty, db_qty,
                    )
                    self._update_db_quantity(db_row["id"], alpaca_qty)
                    result.quantities_updated.append(ticker)

        recovery_cfg: dict[str, Any] = self._config.get("recovery", {}) or {}
        grace_minutes: int = int(
            recovery_cfg.get(
                "fresh_entry_grace_minutes",
                _DEFAULT_FRESH_ENTRY_GRACE_MINUTES,
            )
        )
        now: datetime = self._now_fn()

        for ticker, db_row in db_by_ticker.items():
            if ticker in alpaca_by_ticker:
                continue
            entry_age_min: float | None = _entry_age_minutes(
                db_row.get("entry_time"), now,
            )
            if (
                entry_age_min is not None
                and entry_age_min < float(grace_minutes)
            ):
                # Fresh entry: Alpaca's positions endpoint can lag a fill
                # by several minutes. Closing here would write a phantom
                # exit for a position that very likely just opened. Defer
                # — the next tick will either see it on Alpaca or close
                # it once it's outside the grace window.
                logger.warning(
                    "DB has position %s (entry %.1f min ago) not yet on "
                    "Alpaca — within %d-min fresh-entry grace, deferring "
                    "close",
                    ticker, entry_age_min, grace_minutes,
                )
                result.positions_deferred_fresh.append(ticker)
                continue
            # Exit-in-flight defer: if the DB row indicates an exit
            # order is on the broker (CLOSING / STOP_ACTIVE /
            # TRAILING_ACTIVE) and has a non-NULL order_id to poll,
            # defer the close. `_check_order_statuses` (later in the
            # same tick — see main.tick step 6) will pull the filled
            # order from Alpaca and write the real
            # exit_price/exit_reason/pnl onto the trades row.
            #
            # Without this defer, recovery wins the race and writes
            # `exit_reason='reconciliation_mismatch'` with NULL pnl,
            # silently dropping the realized P&L from
            # `daily_summaries`. Observed 2026-05-15 — see PR #140.
            status_val: str = str(db_row.get("status", "") or "")
            if status_val in _EXIT_IN_FLIGHT_STATUSES:
                tracked_oid: str | None = None
                tracked_col: str | None = None
                for col in _EXIT_ORDER_ID_COLUMNS:
                    val = db_row.get(col)
                    if val:
                        tracked_oid = str(val)
                        tracked_col = col
                        break
                if tracked_oid is not None:
                    logger.info(
                        "DB has position %s (status=%s, %s=%s) not on "
                        "Alpaca — deferring to order-status poll for "
                        "exit attribution",
                        ticker, status_val, tracked_col, tracked_oid,
                    )
                    result.positions_deferred_exit_inflight.append(ticker)
                    continue
            logger.warning(
                "DB has position %s not in Alpaca - marking CLOSED",
                ticker,
            )
            self._close_db_position(db_row["id"], "reconciliation_mismatch")
            result.positions_closed_mismatch.append(ticker)

    # NYSE close = 16:00 ET. DAY stop orders expire at the bell, which
    # means the close-of-day bot tick (cron fires at 20:00 UTC = 16:00 ET)
    # sees an empty open-orders list for those tickers and would replace
    # every fractional position's stop. Alpaca then defers the
    # post-close-submitted stop to the next trading session's pre-market
    # open (~04:00 ET), where it becomes a phantom that holds the qty
    # and blocks the next morning's strategy exit (observed 2026-05-08
    # XLF/XLC, 2026-05-11 09:30 ET overnight_drift exit rejections).
    #
    # Skip placing emergency stops once we're inside the close window;
    # the next session's first tick (09:30 ET) will re-attach a fresh
    # stop in a window where Alpaca submits it immediately.
    _STOP_VERIFY_CUTOFF_HHMM: int = 15 * 60 + 55  # 15:55 ET, used by fallback
    # Minimum seconds before next close required for a DAY stop to make
    # it onto the book before Alpaca defers it to the next session.
    _STOP_VERIFY_CLOSE_BUFFER_SEC: int = 300  # 5 minutes

    async def _inside_stop_placement_window(self) -> bool:
        """Return True when emergency-stop placement is safe.

        Primary: consult Alpaca's clock (`is_open` + `next_close`). This
        handles early-close days (Thanksgiving Friday 13:00 ET, Christmas
        Eve 13:00 ET, July 3 etc.) correctly — the fixed 15:55 cutoff
        would happily place a stop at 13:30 on an early-close day, which
        Alpaca would then defer to the next session and produce the same
        phantom-stop bug this gate exists to prevent.

        Fallback: fixed ET time gate (09:30 ≤ now_et < 15:55). Used only
        when the clock call fails or returns an unrecognised shape — keeps
        the safety net functional even if Alpaca's /v2/clock endpoint
        flakes briefly.
        """
        # Primary path: Alpaca clock.
        try:
            clock = await asyncio.to_thread(self._gw.client.get_clock)
            is_open = getattr(clock, "is_open", None)
            # Strict bool check defends against test gateways whose
            # `get_clock` auto-mocks attributes to MagicMock objects —
            # would otherwise read as truthy and let placement proceed.
            if isinstance(is_open, bool):
                if not is_open:
                    return False
                next_close = getattr(clock, "next_close", None)
                if isinstance(next_close, datetime):
                    if next_close.tzinfo is None:
                        next_close = next_close.replace(tzinfo=ET)
                    seconds_to_close = (
                        next_close - self._now_fn()
                    ).total_seconds()
                    if seconds_to_close < self._STOP_VERIFY_CLOSE_BUFFER_SEC:
                        return False
                return True
        except Exception:
            logger.debug(
                "AlpacaClock unavailable; falling back to fixed time gate",
                exc_info=True,
            )
        # Fallback path: fixed time gate.
        now_et: datetime = self._now_fn()
        hhmm: int = now_et.hour * 60 + now_et.minute
        return 9 * 60 + 30 <= hhmm < self._STOP_VERIFY_CUTOFF_HHMM

    async def _verify_stop_orders(
        self,
        alpaca_positions: list[AlpacaPosition],
        alpaca_orders: list[AlpacaOrder],
        result: RecoveryResult,
    ) -> None:
        """Ensure every open position has an active stop-loss order."""
        if not await self._inside_stop_placement_window():
            # Outside the safe window — defer. Don't even iterate, so
            # the run summary doesn't report "would have placed X stops".
            return

        tickers_with_stops: set[str] = set()
        for order in alpaca_orders:
            if order.type and order.type.value in ("stop", "trailing_stop"):
                tickers_with_stops.add(str(order.symbol))

        exit_cfg: dict[str, Any] = self._config.get("exit_intraday", {})
        default_stop_pct: float = float(exit_cfg.get("stop_loss_pct", 0.02))

        for pos in alpaca_positions:
            ticker: str = str(pos.symbol)
            # Float qty: a fractional Alpaca position truncates to 0 here
            # and the loop would skip ahead, leaving the position
            # unprotected (no emergency stop placed).
            qty: float = float(pos.qty or 0)
            if abs(qty) <= 1e-6:
                continue
            if ticker not in tickers_with_stops:
                logger.warning("No stop order found for %s - placing stop", ticker)
                await self._place_emergency_stop(pos, default_stop_pct)
                result.stops_placed.append(ticker)

    async def _place_emergency_stop(
        self, pos: AlpacaPosition, stop_pct: float,
    ) -> None:
        """Place a stop-loss order for a position that has no stop."""
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, OrderType

        from trading_bot.gateway.order_tif import tif_for_stop

        avg_cost: float = float(pos.avg_entry_price or 0)
        # Float qty: Alpaca holds fractional positions (1/1e6 increments).
        # Truncating to int would refuse to place an emergency stop on any
        # sub-1-share holding.
        qty: float = float(pos.qty or 0)
        ticker: str = str(pos.symbol)

        if avg_cost <= 0:
            logger.error("Cannot place stop for %s: avg_cost is %s", ticker, avg_cost)
            return

        side: OrderSide = OrderSide.SELL if float(pos.qty or 0) > 0 else OrderSide.BUY
        if side == OrderSide.SELL:
            stop_price: float = round(avg_cost * (1.0 - stop_pct), 2)
        else:
            stop_price = round(avg_cost * (1.0 + stop_pct), 2)

        try:
            # Alpaca requires a positive qty; side (SELL for long, BUY for
            # short) determines whether the order closes or opens exposure.
            order_qty: float = abs(qty)
            request = StopOrderRequest(
                symbol=ticker,
                qty=order_qty,
                side=side,
                type=OrderType.STOP,
                time_in_force=tif_for_stop(order_qty),
                stop_price=stop_price,
            )
            # Alpaca SDK is sync — offload to a worker thread so we don't
            # block the event loop on the HTTP submit.
            order = await asyncio.to_thread(
                self._gw.client.submit_order, order_data=request,
            )
            stop_order_id: str = str(getattr(order, "id", "") or "")
            logger.info(
                "Emergency stop placed for %s: %s %.6f @ stop %.2f (id=%s)",
                ticker, side.value, order_qty, stop_price,
                stop_order_id or "?",
            )
            # Without this writeback, ``positions.alpaca_stop_order_id``
            # stays NULL and every subsequent tick sees the position as
            # naked → places another emergency stop → accumulates broker-
            # side stops the bot has no record of. When one of those
            # untracked stops eventually fills, `_check_order_statuses`
            # has no ``order_id`` to poll, so recovery later writes
            # ``exit_reason='reconciliation_mismatch'`` with NULL
            # exit_price/pnl, losing the realized P&L attribution.
            if stop_order_id:
                self._persist_emergency_stop_id(
                    ticker, stop_order_id, stop_price,
                )
        except Exception:
            logger.exception("Failed to place emergency stop for %s", ticker)

    def _persist_emergency_stop_id(
        self,
        ticker: str,
        stop_order_id: str,
        stop_price: float,
    ) -> None:
        """Write the broker-side stop order_id back onto the DB row.

        Targets the unique non-terminal positions row for ``ticker`` that
        is currently missing an ``alpaca_stop_order_id``. ``stop_price``
        is only written when the existing value is NULL — recovery's
        default-stop-pct calculation must not clobber a strategy-set
        target. Multi-row matches log a warning rather than mass-update,
        so a schema accident never blindly applies one order_id to
        multiple positions.
        """
        now: str = datetime.now(tz=ET).isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            try:
                with conn:
                    cur = conn.execute(
                        """SELECT id FROM positions
                            WHERE ticker = ?
                              AND status NOT IN ('CLOSED', 'ENTRY_FAILED')
                              AND alpaca_stop_order_id IS NULL""",
                        (ticker,),
                    )
                    rows = cur.fetchall()
                    if not rows:
                        logger.warning(
                            "Emergency stop persistence: no matching "
                            "POSITION row for %s with NULL "
                            "alpaca_stop_order_id — stop_order_id=%s "
                            "left at broker but untracked",
                            ticker, stop_order_id,
                        )
                        return
                    if len(rows) > 1:
                        logger.warning(
                            "Emergency stop persistence: %d non-terminal "
                            "rows for %s — refusing to write stop_order_id "
                            "to all; manual reconciliation needed",
                            len(rows), ticker,
                        )
                        return
                    position_id: int = int(rows[0][0])
                    conn.execute(
                        """UPDATE positions
                            SET alpaca_stop_order_id = ?,
                                stop_price = COALESCE(stop_price, ?),
                                updated_at = ?
                            WHERE id = ?""",
                        (stop_order_id, stop_price, now, position_id),
                    )
                    logger.info(
                        "Emergency stop persisted: %s position_id=%d "
                        "stop_order_id=%s stop_price=%.4f",
                        ticker, position_id, stop_order_id, stop_price,
                    )
            except sqlite3.OperationalError:
                logger.warning(
                    "Could not persist emergency stop_order_id for %s "
                    "(stop_order_id=%s)",
                    ticker, stop_order_id,
                )
        finally:
            conn.close()

    async def _cancel_stale_entry_orders(
        self,
        alpaca_orders: list[AlpacaOrder],
        result: RecoveryResult,
    ) -> None:
        """Cancel pending entry orders older than the stale threshold.

        Borrowed from alpacahq/example-scalping: a stateless tick missed by
        cron can leave a buy/sell ``new`` order hanging at a price that no
        longer reflects the signal. We cancel any non-stop, non-trailing
        entry order older than ``stale_entry_order_minutes``.
        """
        exit_cfg: dict[str, Any] = self._config.get("exit_intraday", {})
        threshold_min: int = int(
            exit_cfg.get("stale_entry_order_minutes", _DEFAULT_STALE_ENTRY_MINUTES)
        )
        if threshold_min <= 0:
            return

        now: datetime = self._now_fn()
        cutoff: datetime = now - timedelta(minutes=threshold_min)

        for order in alpaca_orders:
            order_type: str = order.type.value if order.type else ""
            # Don't cancel risk-management orders. Those are GTC by design.
            if order_type in ("stop", "trailing_stop", "stop_limit"):
                continue

            submitted_at: datetime | None = _to_et(
                getattr(order, "submitted_at", None)
                or getattr(order, "created_at", None)
            )
            if submitted_at is None or submitted_at >= cutoff:
                continue

            ticker: str = str(order.symbol)
            order_id: str = str(getattr(order, "id", "") or "")
            try:
                await asyncio.to_thread(self._gw.client.cancel_order_by_id, order_id)
                age_min: float = (now - submitted_at).total_seconds() / 60.0
                logger.warning(
                    "Cancelled stale %s order for %s (id=%s, age=%.1f min)",
                    order_type, ticker, order_id, age_min,
                )
                result.stale_orders_cancelled.append(ticker)
            except Exception:
                logger.exception(
                    "Failed to cancel stale order id=%s ticker=%s",
                    order_id, ticker,
                )

    async def _eod_intraday_flatten(
        self,
        alpaca_positions: list[AlpacaPosition],
        alpaca_orders: list[AlpacaOrder],
        result: RecoveryResult,
    ) -> None:
        """Close DB-tagged intraday positions if past the EOD cutoff.

        Swing positions are intentionally untouched. Only intraday-tagged
        positions in the local DB are flattened — that keeps the policy
        decision (which holds are intraday) inside our config rather than
        relying on broker metadata.

        Idempotency (review CRITICAL-4): on a 5-min cron, this method
        runs every tick after 15:55. Without dedup it submits a fresh
        market SELL each time. We dedup on two signals:

        1. DB row status — after a successful submission we mark the
           position ``CLOSING``; ``_intraday_tickers_in_db`` excludes
           it on subsequent ticks.
        2. Alpaca open-orders — if there's already a SELL order pending
           on the ticker (e.g., DB write failed after submit on a
           prior tick), skip. Belt + braces.
        """
        exit_cfg: dict[str, Any] = self._config.get("exit_intraday", {})
        cutoff_str: str = str(
            exit_cfg.get("eod_flatten_time_et", _DEFAULT_EOD_FLATTEN_TIME)
        )
        if not cutoff_str:
            return

        try:
            cutoff_t: time = time.fromisoformat(cutoff_str)
        except ValueError:
            logger.warning("Invalid eod_flatten_time_et=%r — skipping", cutoff_str)
            return

        now_et: datetime = self._now_fn()
        if now_et.time() < cutoff_t:
            return

        intraday_tickers: set[str] = self._intraday_tickers_in_db()
        if not intraday_tickers:
            return

        # Tickers that already have a pending SELL on Alpaca — covers
        # the rare case where a prior tick submitted but then crashed
        # before we marked the DB row CLOSING.
        pending_sell_tickers: set[str] = _tickers_with_pending_sell(alpaca_orders)

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        for pos in alpaca_positions:
            ticker: str = str(pos.symbol)
            if ticker not in intraday_tickers:
                continue
            if ticker in pending_sell_tickers:
                logger.info(
                    "EOD flatten skip %s: pending close order already on "
                    "Alpaca (idempotency)",
                    ticker,
                )
                continue
            # Float qty: a fractional Alpaca position truncated to int
            # would be zeroed and skipped here — the EOD flatten path
            # would silently leave it open overnight, exposing the bot
            # to gap risk it was specifically designed to avoid.
            qty: float = float(pos.qty or 0)
            if abs(qty) <= 1e-6:
                continue

            side: OrderSide = OrderSide.SELL if qty > 0 else OrderSide.BUY
            try:
                request = MarketOrderRequest(
                    symbol=ticker,
                    qty=abs(qty),
                    side=side,
                    time_in_force=TimeInForce.DAY,
                )
                await asyncio.to_thread(self._gw.client.submit_order, order_data=request)
                logger.warning(
                    "EOD flatten: %s %.6f %s (cutoff=%s)",
                    side.value, abs(qty), ticker, cutoff_str,
                )
                result.eod_flatten_orders.append(ticker)
                # Mark CLOSING in the DB so the next tick's
                # _intraday_tickers_in_db excludes this ticker. Done in
                # the same try block so a DB error here doesn't double
                # the order — the next tick's pending-sell check will
                # still catch it.
                self._mark_intraday_closing(ticker)
            except Exception:
                logger.exception("EOD flatten failed for %s", ticker)

    def _intraday_tickers_in_db(self) -> set[str]:
        """Tickers in the DB that are intraday-tagged AND still need an
        EOD flatten. Excludes CLOSED, ENTRY_FAILED, AND CLOSING — the
        last is what makes EOD flatten idempotent across ticks.
        """
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ticker FROM positions "
                "WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED', 'CLOSING') "
                "AND hold_type = 'intraday'"
            )
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()
        finally:
            conn.close()

    def _mark_intraday_closing(self, ticker: str) -> None:
        """Transition all open intraday positions for ``ticker`` to
        CLOSING. Called after a successful EOD flatten submission so
        subsequent ticks skip the same row."""
        now: str = datetime.now(tz=ET).isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE positions SET status = 'CLOSING', updated_at = ? "
                "WHERE ticker = ? AND hold_type = 'intraday' "
                "AND status NOT IN ('CLOSED', 'ENTRY_FAILED', 'CLOSING')",
                (now, ticker),
            )
            conn.commit()
        except sqlite3.OperationalError:
            logger.warning(
                "EOD flatten: failed to mark %s CLOSING in DB; pending-sell "
                "check on next tick will still suppress duplicates",
                ticker,
            )
        finally:
            conn.close()

    # Window for matching an Alpaca-discovered position against a recent
    # ENTRY_FAILED row. Long enough to absorb the 5-min entry-pending
    # sweep + reconciliation tick cadence, short enough that we don't
    # accidentally merge across distinct entries for the same ticker.
    _ORPHAN_REATTRIBUTION_WINDOW_MIN: int = 30
    # Wider window for healing already-created 'unknown' orphans on
    # subsequent ticks — covers overnight gaps when the orphan was
    # created at the close and the matching ENTRY_FAILED row from a
    # different strategy is up to a few sessions old.
    _ORPHAN_HEAL_WINDOW_HOURS: int = 48
    # Tolerance for entry-price match when reattributing. Limit orders
    # fill at our exact limit price in normal conditions; allow 1¢ of
    # rounding/quote-tick drift.
    _ORPHAN_REATTRIBUTION_PRICE_TOL: float = 0.01

    def _heal_unknown_orphans(self, result: RecoveryResult) -> None:
        """Reattribute existing ``strategy_id='unknown'`` POSITION_OPEN rows.

        When a previous tick created an orphan from an Alpaca position
        because the matching local entry had already flipped to
        ENTRY_FAILED, we can heal the attribution on the next tick by
        merging the two rows. The ENTRY_FAILED row is the canonical one
        (it carries the original ``alpaca_order_id`` and the correct
        strategy_id); we promote it to POSITION_OPEN and close out the
        unknown orphan with ``exit_reason='reattributed'``.

        Idempotent: safe to call on every tick. If no orphan/ENTRY_FAILED
        pair matches, this is a no-op.
        """
        cutoff: str = (
            datetime.now(tz=ET) - timedelta(hours=self._ORPHAN_HEAL_WINDOW_HOURS)
        ).isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Pull every active orphan in the heal window.
            orphans = conn.execute(
                "SELECT id, ticker, quantity, entry_price, entry_time "
                "FROM positions "
                "WHERE status = 'POSITION_OPEN' AND strategy_id = 'unknown' "
                "AND entry_time >= ?",
                (cutoff,),
            ).fetchall()
            if not orphans:
                return
            healed: list[str] = []
            now: str = datetime.now(tz=ET).isoformat()
            for orphan in orphans:
                orphan_id: int = int(orphan["id"])
                # Atomic claim: promote a matching ENTRY_FAILED row to
                # POSITION_OPEN in a single UPDATE that selects on the
                # match predicates. If another tick has already claimed
                # the same ENTRY_FAILED row (status no longer matches),
                # rowcount = 0 and we skip without producing a duplicate
                # heal. Without this, the SELECT-then-UPDATE pattern had
                # a race window where two ticks could both observe the
                # ENTRY_FAILED row and both attempt to promote it /
                # close the orphan, inflating the heal count.
                with conn:
                    cur = conn.execute(
                        "UPDATE positions "
                        "SET status = 'POSITION_OPEN', updated_at = ? "
                        "WHERE status = 'ENTRY_FAILED' AND ticker = ? "
                        "AND ABS(quantity - ?) < 1e-6 "
                        "AND ABS(entry_price - ?) <= ? "
                        "AND entry_time >= ? "
                        "AND strategy_id IS NOT NULL AND strategy_id != 'unknown' "
                        "AND id = ("
                        "    SELECT id FROM positions "
                        "    WHERE status = 'ENTRY_FAILED' AND ticker = ? "
                        "    AND ABS(quantity - ?) < 1e-6 "
                        "    AND ABS(entry_price - ?) <= ? "
                        "    AND entry_time >= ? "
                        "    AND strategy_id IS NOT NULL AND strategy_id != 'unknown' "
                        "    ORDER BY entry_time DESC LIMIT 1"
                        ")",
                        (
                            now,
                            orphan["ticker"],
                            float(orphan["quantity"]),
                            float(orphan["entry_price"]),
                            self._ORPHAN_REATTRIBUTION_PRICE_TOL,
                            cutoff,
                            orphan["ticker"],
                            float(orphan["quantity"]),
                            float(orphan["entry_price"]),
                            self._ORPHAN_REATTRIBUTION_PRICE_TOL,
                            cutoff,
                        ),
                    )
                    if cur.rowcount == 0:
                        # No matching ENTRY_FAILED row (or another tick
                        # claimed it first). Leave the orphan alone.
                        continue
                    # Look up the row we just promoted so we can record
                    # the canonical id + strategy in the heal summary.
                    promoted = conn.execute(
                        "SELECT id, strategy_id FROM positions "
                        "WHERE ticker = ? AND status = 'POSITION_OPEN' "
                        "AND ABS(quantity - ?) < 1e-6 "
                        "AND ABS(entry_price - ?) <= ? "
                        "AND id != ? "
                        "AND updated_at = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (
                            orphan["ticker"],
                            float(orphan["quantity"]),
                            float(orphan["entry_price"]),
                            self._ORPHAN_REATTRIBUTION_PRICE_TOL,
                            orphan_id,
                            now,
                        ),
                    ).fetchone()
                    if promoted is None:
                        # Promotion happened but lookup didn't find it
                        # (extremely unlikely — would indicate concurrent
                        # mutation). Log and bail without closing the
                        # orphan, to avoid leaving an open Alpaca holding
                        # with no DB row.
                        logger.error(
                            "Healed orphan attribution: %s promoted but "
                            "canonical row lookup failed; not closing "
                            "orphan #%d.",
                            orphan["ticker"], orphan_id,
                        )
                        continue
                    canonical_id = int(promoted["id"])
                    strategy_id = str(promoted["strategy_id"])
                    conn.execute(
                        "UPDATE positions SET status = 'CLOSED', "
                        "updated_at = ? WHERE id = ?",
                        (now, orphan_id),
                    )
                healed.append(
                    f"{orphan['ticker']} #{orphan_id}→#{canonical_id} ({strategy_id})"
                )
                logger.warning(
                    "Healed orphan attribution: %s qty=%.6f price=%.4f "
                    "— merged unknown #%d into ENTRY_FAILED #%d (strategy=%s).",
                    orphan["ticker"], float(orphan["quantity"]),
                    float(orphan["entry_price"]),
                    orphan_id, canonical_id, strategy_id,
                )
            result.orphans_healed.extend(healed)
        except sqlite3.OperationalError:
            logger.warning("Could not heal unknown orphans (DB unavailable)")
        finally:
            conn.close()

    def _try_reattribute_entry_failed(
        self,
        conn: sqlite3.Connection,
        ticker: str,
        qty: float,
        avg_cost: float,
        now_iso: str,
    ) -> bool:
        """Attempt to merge an Alpaca-discovered position into a recent
        ENTRY_FAILED row instead of creating a new ``strategy_id='unknown'``
        orphan.

        Returns True if a matching row was reattributed. False otherwise.

        Match criteria (all must hold):
        - status = 'ENTRY_FAILED'
        - ticker matches
        - quantity within 1e-6 (Alpaca fractional precision)
        - entry_price within ``_ORPHAN_REATTRIBUTION_PRICE_TOL``
        - row created within ``_ORPHAN_REATTRIBUTION_WINDOW_MIN``

        Rationale: 2026-05-11 XLK fill in 169 ms, but the local 5-min
        entry-pending sweep timed out anyway and flipped the row to
        ENTRY_FAILED. The next reconciliation tick then saw the
        unattached Alpaca position and created a duplicate row with
        ``strategy_id='unknown'``, silently breaking the overnight_drift
        exit logic. Treat the ENTRY_FAILED row as the canonical record
        whenever the Alpaca position is clearly the same entry.
        """
        cutoff: str = (
            datetime.now(tz=ET) - timedelta(minutes=self._ORPHAN_REATTRIBUTION_WINDOW_MIN)
        ).isoformat()
        # Atomic claim: single UPDATE filtered on the match predicates,
        # with rowcount = 0 signalling "nothing to merge". This collapses
        # the previous SELECT-then-UPDATE pair into a single statement
        # so two concurrent ticks can't both observe the ENTRY_FAILED
        # row and both promote it (the second tick's UPDATE would see
        # status no longer matching 'ENTRY_FAILED' and report 0 rows).
        with conn:
            cur = conn.execute(
                "UPDATE positions "
                "SET status = 'POSITION_OPEN', updated_at = ? "
                "WHERE ticker = ? AND status = 'ENTRY_FAILED' "
                "AND ABS(quantity - ?) < 1e-6 "
                "AND ABS(entry_price - ?) <= ? "
                "AND entry_time >= ? "
                "AND id = ("
                "    SELECT id FROM positions "
                "    WHERE ticker = ? AND status = 'ENTRY_FAILED' "
                "    AND ABS(quantity - ?) < 1e-6 "
                "    AND ABS(entry_price - ?) <= ? "
                "    AND entry_time >= ? "
                "    ORDER BY entry_time DESC LIMIT 1"
                ")",
                (
                    now_iso,
                    ticker, qty, avg_cost,
                    self._ORPHAN_REATTRIBUTION_PRICE_TOL, cutoff,
                    ticker, qty, avg_cost,
                    self._ORPHAN_REATTRIBUTION_PRICE_TOL, cutoff,
                ),
            )
            if cur.rowcount == 0:
                return False
        # Best-effort strategy_id lookup for the log line. The match
        # criteria are tight enough that the row we just promoted is the
        # only one fitting the description with updated_at = now_iso.
        row = conn.execute(
            "SELECT id, strategy_id FROM positions "
            "WHERE ticker = ? AND status = 'POSITION_OPEN' "
            "AND ABS(quantity - ?) < 1e-6 "
            "AND ABS(entry_price - ?) <= ? "
            "AND updated_at = ? "
            "ORDER BY id DESC LIMIT 1",
            (ticker, qty, avg_cost, self._ORPHAN_REATTRIBUTION_PRICE_TOL, now_iso),
        ).fetchone()
        row_id: int = int(row[0]) if row is not None else -1
        strategy_id: str = (
            str(row[1]) if row is not None and row[1] is not None else "unknown"
        )
        logger.warning(
            "Reattributed Alpaca position %s (qty=%.6f, price=%.4f) to "
            "ENTRY_FAILED row #%d (strategy=%s) — entry filled at broker "
            "but local marker timed out. See feedback_per_strategy_vs_broker_truth.",
            ticker, qty, avg_cost, row_id, strategy_id,
        )
        return True

    def _create_db_position(self, ticker: str, pos: AlpacaPosition) -> None:
        now: str = datetime.now(tz=ET).isoformat()
        avg_cost: float = float(pos.avg_entry_price or 0)
        # Float qty: Alpaca holds fractional positions; truncating an Alpaca-
        # discovered position to int would write the DB row as 0 shares,
        # which the exit path then refuses to close.
        qty: float = float(pos.qty or 0)

        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            # Prefer reattributing a recent ENTRY_FAILED row over creating
            # a fresh strategy_id='unknown' orphan. Same broker fill,
            # known strategy attribution.
            if self._try_reattribute_entry_failed(
                conn, ticker, qty, avg_cost, now,
            ):
                return
            conn.execute(
                """INSERT INTO positions
                   (ticker, exchange, currency, quantity, entry_price,
                    entry_time, status, hold_type, phase, strategy_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'POSITION_OPEN', 'swing', 1, 'unknown')""",
                (ticker, str(pos.exchange or "US"), "USD", qty, avg_cost, now),
            )
            conn.commit()
            logger.info("Created DB position for %s: qty=%.6f, price=%.4f", ticker, qty, avg_cost)
        except sqlite3.OperationalError:
            logger.warning("Could not create position for %s", ticker)
        finally:
            conn.close()

    def _update_db_quantity(self, position_id: int, new_qty: float) -> None:
        now: str = datetime.now(tz=ET).isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE positions SET quantity = ?, updated_at = ? WHERE id = ?",
                (new_qty, now, position_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _close_db_position(self, position_id: int, reason: str) -> None:
        """Mark a DB-only position CLOSED and stamp a matching trades row.

        Phase 3 B4 fix: the trades INSERT now carries the position's
        strategy_id, derives ``side`` from the quantity sign (positive →
        BUY long entry, negative → SELL short entry — the schema stores
        the entry direction; ``exit_reason`` captures the close), and
        leaves ``exit_price``/``net_pnl`` NULL so the alpaca_backfill
        tool can populate them from order history. Pre-fix hardcoded
        side='SELL' made every recovery-driven trade look like a short
        entry, with no strategy attribution at all.

        Issue #132 fix: prefer UPDATE-on-existing-entry-row over INSERT
        of a stub. Most reconciliation-mismatch closes hit a position
        that was created by ``OrderManager._create_position_record`` —
        which already wrote an entry trades row carrying the strategy
        metadata. INSERT-ing a second row produced 25% duplicates on
        ``(ticker, entry_time, strategy_id)`` in prod and forced the
        hydration JOIN to lean on ``MIN(t.id)`` for determinism (#128).
        Now: locate the entry row (``exit_time IS NULL`` with matching
        key) and update its exit columns. Only INSERT when no entry
        row exists — the legitimate case for positions discovered by
        ``_create_db_position`` from a stray Alpaca holding.
        """
        now: str = datetime.now(tz=ET).isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        # Use Row factory so the audit-table INSERT below references columns
        # by name. Positional indexing into ``row[1]…row[14]`` breaks
        # silently when the positions table schema gets reordered or
        # extended by a future migration.
        conn.row_factory = sqlite3.Row
        try:
            # Explicit ``with conn:`` makes atomicity self-documenting:
            # both the UPDATE and the INSERT either commit together or
            # roll back together if any statement raises.
            try:
                with conn:
                    conn.execute(
                        "UPDATE positions SET status = 'CLOSED', "
                        "updated_at = ? WHERE id = ?",
                        (now, position_id),
                    )
                    row = conn.execute(
                        "SELECT * FROM positions WHERE id = ?", (position_id,)
                    ).fetchone()
                    if row:
                        branch, trade_id = close_or_insert_trade_row(
                            conn,
                            position_row=row,
                            exit_time=now,
                            exit_reason=reason,
                            notes=(
                                "Auto-closed by state recovery; "
                                "exit_price/net_pnl pending "
                                "alpaca_backfill"
                            ),
                        )
                        logger.info(
                            "Recovery close: position_id=%d trades.id=%d "
                            "branch=%s reason=%s",
                            position_id, trade_id, branch, reason,
                        )
            except sqlite3.OperationalError:
                logger.warning("Could not close position id=%d", position_id)
        finally:
            conn.close()


def _tickers_with_pending_sell(alpaca_orders: list[AlpacaOrder]) -> set[str]:
    """Tickers that already have a non-terminal SELL order on Alpaca.

    Used by EOD flatten to dedup — if a previous tick submitted but
    crashed before persisting state, we'd otherwise double-submit.
    """
    pending: set[str] = set()
    open_statuses = {
        "new",
        "accepted",
        "pending_new",
        "accepted_for_bidding",
        "partially_filled",
        "pending_replace",
        "pending_cancel",
    }
    for order in alpaca_orders:
        side_obj = getattr(order, "side", None)
        side_val: str = (
            getattr(side_obj, "value", "") if side_obj is not None else ""
        )
        status_obj = getattr(order, "status", None)
        status_val: str = (
            getattr(status_obj, "value", "") if status_obj is not None else ""
        )
        if side_val.lower() == "sell" and status_val.lower() in open_statuses:
            pending.add(str(order.symbol))
    return pending


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _to_et(value: Any) -> datetime | None:
    """Coerce an Alpaca timestamp (datetime or ISO string) to ET, or None."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt: datetime = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(ET)


def _entry_age_minutes(entry_time: Any, now: datetime) -> float | None:
    """Minutes elapsed since ``entry_time``, or None if unparseable.

    Used by the fresh-entry grace check in ``_reconcile``: a position
    whose entry_time is recent enough may not have shown up on Alpaca's
    positions endpoint yet, so the mismatch branch should defer rather
    than close it.
    """
    et_dt = _to_et(entry_time)
    if et_dt is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return (now - et_dt).total_seconds() / 60.0

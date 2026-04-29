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
from datetime import date, datetime, time, timedelta
from typing import Any, Callable
from zoneinfo import ZoneInfo

from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import Order as AlpacaOrder

from trading_bot.constants import TZ_EASTERN
from trading_bot.gateway.connection import GatewayConnection
from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN

# Default thresholds — overridable via config.
_DEFAULT_STALE_ENTRY_MINUTES: int = 5
_DEFAULT_EOD_FLATTEN_TIME: str = "15:55"


@dataclass
class RecoveryResult:
    """Summary of the startup state-recovery process."""

    broker_positions: int = 0
    broker_open_orders: int = 0
    db_open_positions: int = 0

    positions_created: list[str] = field(default_factory=list)
    positions_closed_mismatch: list[str] = field(default_factory=list)
    quantities_updated: list[str] = field(default_factory=list)
    stops_placed: list[str] = field(default_factory=list)
    settlements_updated: int = 0
    stale_orders_cancelled: list[str] = field(default_factory=list)
    eod_flatten_orders: list[str] = field(default_factory=list)

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
        if self.settlements_updated:
            lines.append(f"Settlements marked settled: {self.settlements_updated}")
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

        # 4. Load SQLite open positions
        db_positions: list[dict[str, Any]] = self._load_db_open_positions()
        result.db_open_positions = len(db_positions)

        # 5. Reconcile
        self._reconcile(alpaca_positions, db_positions, result)

        # 6. Verify stop orders
        await self._verify_stop_orders(alpaca_positions, alpaca_orders, result)

        # 7. Cancel stale entry orders. Fresh order list because step 6 may
        #    have placed new stops, but those are GTC and not stale.
        await self._cancel_stale_entry_orders(alpaca_orders, result)

        # 8. EOD intraday flatten. Closes intraday-tagged DB positions if the
        #    cron tick lands after the configured cutoff (default 15:55 ET).
        await self._eod_intraday_flatten(alpaca_positions, result)

        # 9. Update settlements
        result.settlements_updated = self._update_settlements()

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
            cursor = conn.execute("SELECT * FROM positions WHERE status != 'CLOSED'")
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
            qty: int = int(float(pos.qty or 0))
            if qty != 0:
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
                alpaca_qty: int = int(float(pos.qty or 0))
                db_qty: int = int(db_row.get("quantity", 0))
                if alpaca_qty != db_qty:
                    logger.warning(
                        "Quantity mismatch for %s: Alpaca=%d, DB=%d. Updating DB.",
                        ticker, alpaca_qty, db_qty,
                    )
                    self._update_db_quantity(db_row["id"], alpaca_qty)
                    result.quantities_updated.append(ticker)

        for ticker, db_row in db_by_ticker.items():
            if ticker not in alpaca_by_ticker:
                logger.warning(
                    "DB has position %s not in Alpaca - marking CLOSED",
                    ticker,
                )
                self._close_db_position(db_row["id"], "reconciliation_mismatch")
                result.positions_closed_mismatch.append(ticker)

    async def _verify_stop_orders(
        self,
        alpaca_positions: list[AlpacaPosition],
        alpaca_orders: list[AlpacaOrder],
        result: RecoveryResult,
    ) -> None:
        """Ensure every open position has an active stop-loss order."""
        tickers_with_stops: set[str] = set()
        for order in alpaca_orders:
            if order.type and order.type.value in ("stop", "trailing_stop"):
                tickers_with_stops.add(str(order.symbol))

        exit_cfg: dict[str, Any] = self._config.get("exit_intraday", {})
        default_stop_pct: float = float(exit_cfg.get("stop_loss_pct", 0.02))

        for pos in alpaca_positions:
            ticker: str = str(pos.symbol)
            qty: int = int(float(pos.qty or 0))
            if qty == 0:
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
        from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

        avg_cost: float = float(pos.avg_entry_price or 0)
        qty: int = int(float(pos.qty or 0))
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
            order_qty: int = abs(qty)
            request = StopOrderRequest(
                symbol=ticker,
                qty=order_qty,
                side=side,
                type=OrderType.STOP,
                time_in_force=TimeInForce.GTC,
                stop_price=stop_price,
            )
            # Alpaca SDK is sync — offload to a worker thread so we don't
            # block the event loop on the HTTP submit.
            await asyncio.to_thread(self._gw.client.submit_order, order_data=request)
            logger.info("Emergency stop placed for %s: %s %d @ stop %.2f", ticker, side.value, order_qty, stop_price)
        except Exception:
            logger.exception("Failed to place emergency stop for %s", ticker)

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
        result: RecoveryResult,
    ) -> None:
        """Close DB-tagged intraday positions if past the EOD cutoff.

        Swing positions are intentionally untouched. Only intraday-tagged
        positions in the local DB are flattened — that keeps the policy
        decision (which holds are intraday) inside our config rather than
        relying on broker metadata.
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

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        for pos in alpaca_positions:
            ticker: str = str(pos.symbol)
            if ticker not in intraday_tickers:
                continue
            qty: int = int(float(pos.qty or 0))
            if qty == 0:
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
                    "EOD flatten: %s %d %s (cutoff=%s)",
                    side.value, abs(qty), ticker, cutoff_str,
                )
                result.eod_flatten_orders.append(ticker)
            except Exception:
                logger.exception("EOD flatten failed for %s", ticker)

    def _intraday_tickers_in_db(self) -> set[str]:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ticker FROM positions "
                "WHERE status != 'CLOSED' AND hold_type = 'intraday'"
            )
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()
        finally:
            conn.close()

    def _update_settlements(self) -> int:
        today_str: str = date.today().isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "UPDATE settlements SET settled = 1 WHERE settled = 0 AND settle_date <= ?",
                (today_str,),
            )
            count: int = cursor.rowcount
            conn.commit()
            if count:
                logger.info("Marked %d settlements as settled", count)
            return count
        except sqlite3.OperationalError:
            logger.warning("settlements table not found - skipping update")
            return 0
        finally:
            conn.close()

    def _create_db_position(self, ticker: str, pos: AlpacaPosition) -> None:
        now: str = datetime.now(tz=ET).isoformat()
        avg_cost: float = float(pos.avg_entry_price or 0)
        qty: int = int(float(pos.qty or 0))

        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO positions
                   (ticker, exchange, currency, quantity, entry_price,
                    entry_time, status, hold_type, phase, strategy_id)
                   VALUES (?, ?, ?, ?, ?, ?, 'POSITION_OPEN', 'swing', 1, 'unknown')""",
                (ticker, str(pos.exchange or "US"), "USD", qty, avg_cost, now),
            )
            conn.commit()
            logger.info("Created DB position for %s: qty=%d, price=%.4f", ticker, qty, avg_cost)
        except sqlite3.OperationalError:
            logger.warning("Could not create position for %s", ticker)
        finally:
            conn.close()

    def _update_db_quantity(self, position_id: int, new_qty: int) -> None:
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
                        conn.execute(
                            """INSERT INTO trades
                               (ticker, exchange, currency, side, entry_time,
                                entry_price, quantity, exit_time, exit_reason,
                                hold_type, phase, notes)
                               VALUES (:ticker, :exchange, :currency, 'SELL',
                                       :entry_time, :entry_price, :quantity,
                                       :exit_time, :exit_reason, :hold_type,
                                       :phase,
                                       'Auto-closed by state recovery')""",
                            {
                                "ticker": row["ticker"],
                                "exchange": row["exchange"],
                                "currency": row["currency"],
                                "entry_time": row["entry_time"],
                                "entry_price": row["entry_price"],
                                "quantity": row["quantity"],
                                "exit_time": now,
                                "exit_reason": reason,
                                "hold_type": row["hold_type"],
                                "phase": row["phase"],
                            },
                        )
            except sqlite3.OperationalError:
                logger.warning("Could not close position id=%d", position_id)
        finally:
            conn.close()


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

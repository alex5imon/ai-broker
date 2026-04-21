"""State recovery and reconciliation on startup.

Alpaca is the source of truth.  On every startup the bot queries Alpaca
for positions, orders, and account state, then reconciles with the local
SQLite database before resuming normal operation.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import Order as AlpacaOrder

from trading_bot.gateway.connection import GatewayConnection
from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")


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

    account_equity: float = 0.0
    settled_cash: float = 0.0
    buying_power: float = 0.0

    @property
    def has_discrepancies(self) -> bool:
        return bool(
            self.positions_created
            or self.positions_closed_mismatch
            or self.quantities_updated
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
    ) -> None:
        self._gw: GatewayConnection = gateway
        self._db_path: str = db_path
        self._notifier: Notifier = notifier
        self._config: dict[str, Any] = config or {}

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

        # 7. Update settlements
        result.settlements_updated = self._update_settlements()

        # 8. Log and alert
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
            request = StopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=side,
                type=OrderType.STOP,
                time_in_force=TimeInForce.GTC,
                stop_price=stop_price,
            )
            self._gw.client.submit_order(order_data=request)
            logger.info("Emergency stop placed for %s: %s %d @ stop %.2f", ticker, side.value, qty, stop_price)
        except Exception:
            logger.exception("Failed to place emergency stop for %s", ticker)

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
        try:
            conn.execute(
                "UPDATE positions SET status = 'CLOSED', updated_at = ? WHERE id = ?",
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
                       VALUES (?, ?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?,
                               'Auto-closed by state recovery')""",
                    (
                        row[1], row[2], row[3], row[7], row[6],
                        row[5], now, reason, row[13], row[14],
                    ),
                )
            conn.commit()
        except sqlite3.OperationalError:
            logger.warning("Could not close position id=%d", position_id)
        finally:
            conn.close()


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

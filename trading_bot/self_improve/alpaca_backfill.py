"""One-shot backfill: reconstruct closed trades from Alpaca order history.

The live bot writes entries into the ``trades`` table but does not currently
persist exit data (see follow-up doc ``docs/self_improve_followups.md``).
Strategy attribution survives in the ``positions`` table, which holds
``alpaca_*_order_id`` for the entry, stop, target, and trailing orders.

This script:
  1. Loads closed positions with a known strategy_id.
  2. Skips positions that already have a backfilled ``trades`` row
     (idempotent — re-running is safe).
  3. Pulls Alpaca order history for each ticker around the entry time.
  4. Pairs the entry with its first matching SELL fill (FIFO, by quantity).
  5. Infers ``exit_reason`` by matching the SELL order id against the
     position's stop/target/trail order ids.
  6. Writes a complete row into ``trades`` with a ``notes`` marker so it
     can be detected on subsequent runs.

Net P&L is gross P&L (Alpaca equities are commission-free). FX rate is
1.0 (USD account). Slippage is left null — entry price is from the
position record, exit from the Alpaca fill.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

logger = logging.getLogger(__name__)


# Window after entry_time within which we look for the matching exit fill.
# Overnight drift positions exit on the next session's open (~18 hours);
# mean reversion exits intraday or by wind_down. 7 days is a comfortable
# upper bound — anything older than that is a stale position the
# reconciliation path should surface separately.
EXIT_LOOKBACK_DAYS: int = 7

# Notes-column marker so we can detect already-backfilled rows. Format:
# ``backfill:position:{position_id}``.
BACKFILL_MARKER_PREFIX: str = "backfill:position:"


@dataclass(frozen=True)
class ClosedPositionRow:
    """Row from the ``positions`` table that needs a matching ``trades`` row."""

    position_id: int
    ticker: str
    exchange: str
    currency: str
    strategy_id: str
    quantity: int
    entry_price: float
    entry_time: datetime
    hold_type: str
    phase: int
    alpaca_order_id: str | None
    alpaca_stop_order_id: str | None
    alpaca_target_order_id: str | None
    alpaca_trail_order_id: str | None


@dataclass(frozen=True)
class ExitFill:
    """The Alpaca SELL fill paired to a closed position."""

    order_id: str
    filled_at: datetime
    filled_avg_price: float
    filled_qty: float


@dataclass(frozen=True)
class BackfillReport:
    """Summary of a single backfill invocation."""

    candidates_found: int
    already_backfilled: int
    no_exit_found: int
    inserted: int
    dry_run: bool


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def load_candidates(conn: sqlite3.Connection) -> list[ClosedPositionRow]:
    """Closed positions with a strategy_id, not yet backfilled."""
    cur = conn.execute(
        f"""
        SELECT p.id, p.ticker, p.exchange, p.currency, p.strategy_id,
               p.quantity, p.entry_price, p.entry_time, p.hold_type, p.phase,
               p.alpaca_order_id, p.alpaca_stop_order_id,
               p.alpaca_target_order_id, p.alpaca_trail_order_id
          FROM positions p
         WHERE p.status = 'CLOSED'
           AND p.strategy_id IS NOT NULL
           AND p.strategy_id != 'unknown'
           AND NOT EXISTS (
                 SELECT 1 FROM trades t
                  WHERE t.notes = '{BACKFILL_MARKER_PREFIX}' || p.id
               )
         ORDER BY p.entry_time
        """
    )
    out: list[ClosedPositionRow] = []
    for row in cur.fetchall():
        entry_dt = _parse_iso(row[7])
        if entry_dt is None:
            logger.warning("Position %d has unparseable entry_time %r — skipping",
                           row[0], row[7])
            continue
        out.append(
            ClosedPositionRow(
                position_id=row[0],
                ticker=row[1],
                exchange=row[2],
                currency=row[3],
                strategy_id=row[4],
                quantity=int(row[5]),
                entry_price=float(row[6]),
                entry_time=entry_dt,
                hold_type=row[8],
                phase=int(row[9]),
                alpaca_order_id=row[10],
                alpaca_stop_order_id=row[11],
                alpaca_target_order_id=row[12],
                alpaca_trail_order_id=row[13],
            )
        )
    return out


def _infer_exit_reason(exit_order_id: str, position: ClosedPositionRow) -> str:
    """Map a SELL order id back to its semantic exit reason.

    Returns ``manual`` when no match — the live bot also flattens via
    market sells in wind-down and emergency paths, which would not match
    any of the precomputed bracket-order ids.
    """
    if position.alpaca_stop_order_id and exit_order_id == position.alpaca_stop_order_id:
        return "stop_loss"
    if position.alpaca_target_order_id and exit_order_id == position.alpaca_target_order_id:
        return "take_profit"
    if position.alpaca_trail_order_id and exit_order_id == position.alpaca_trail_order_id:
        return "trailing_stop"
    return "manual"


async def find_exit_fill(
    client,  # alpaca.trading.client.TradingClient
    position: ClosedPositionRow,
    *,
    lookback_days: int = EXIT_LOOKBACK_DAYS,
) -> ExitFill | None:
    """Pull SELL fills for ``position.ticker`` after entry, return first match.

    "Matches" = side=SELL, filled, and ``filled_qty`` is at least the
    position's quantity (Alpaca may split a sell across fills, but
    ``get_orders`` returns the parent order with rolled-up totals).
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus, OrderSide

    after = position.entry_time
    until = after + timedelta(days=lookback_days)
    request = GetOrdersRequest(
        status=QueryOrderStatus.CLOSED,
        symbols=[position.ticker],
        after=after,
        until=until,
        side=OrderSide.SELL,
        limit=50,
    )
    try:
        orders = await asyncio.to_thread(client.get_orders, filter=request)
    except Exception:
        logger.exception("Alpaca get_orders failed for position %d (%s)",
                         position.position_id, position.ticker)
        return None

    for order in orders:
        if str(getattr(order, "status", "")) not in ("OrderStatus.FILLED", "filled"):
            # alpaca-py returns enum values; tolerate both .value and str()
            status_str = str(getattr(order, "status", "")).lower()
            if "filled" not in status_str:
                continue
        filled_qty_raw = getattr(order, "filled_qty", None)
        filled_price_raw = getattr(order, "filled_avg_price", None)
        filled_at_raw = getattr(order, "filled_at", None)
        if filled_qty_raw is None or filled_price_raw is None or filled_at_raw is None:
            continue
        try:
            filled_qty = float(filled_qty_raw)
            filled_price = float(filled_price_raw)
        except (TypeError, ValueError):
            continue
        if filled_qty + 1e-6 < position.quantity:
            continue
        filled_at = filled_at_raw if isinstance(filled_at_raw, datetime) else _parse_iso(str(filled_at_raw))
        if filled_at is None:
            continue
        return ExitFill(
            order_id=str(order.id),
            filled_at=filled_at,
            filled_avg_price=filled_price,
            filled_qty=filled_qty,
        )

    return None


def insert_backfilled_trade(
    conn: sqlite3.Connection,
    position: ClosedPositionRow,
    exit_fill: ExitFill,
) -> None:
    """Write a complete trades row, idempotent via the notes marker."""
    gross_pnl = (exit_fill.filled_avg_price - position.entry_price) * position.quantity
    exit_reason = _infer_exit_reason(exit_fill.order_id, position)
    note = f"{BACKFILL_MARKER_PREFIX}{position.position_id}"

    conn.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side,
            entry_time, entry_price, quantity,
            exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, pnl_gbp, fx_rate,
            hold_type, phase, strategy_id, notes
        )
        VALUES (?, ?, ?, 'long',
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, 1.0,
                ?, ?, ?, ?)
        """,
        (
            position.ticker, position.exchange, position.currency,
            position.entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            position.entry_price, position.quantity,
            exit_fill.filled_at.strftime("%Y-%m-%d %H:%M:%S"),
            exit_fill.filled_avg_price, exit_reason,
            gross_pnl, gross_pnl, gross_pnl,
            position.hold_type, position.phase, position.strategy_id, note,
        ),
    )


async def backfill(
    conn: sqlite3.Connection,
    client,  # alpaca.trading.client.TradingClient
    *,
    dry_run: bool = False,
    fill_finder=None,  # injectable for tests
) -> BackfillReport:
    """Pair every eligible closed position with its Alpaca exit fill."""
    candidates = load_candidates(conn)
    logger.info("Found %d candidate position(s) to backfill", len(candidates))

    finder = fill_finder or find_exit_fill
    inserted = 0
    no_exit = 0

    for c in candidates:
        exit_fill = await finder(client, c)
        if exit_fill is None:
            logger.warning(
                "No exit fill found for position %d (%s entered %s)",
                c.position_id, c.ticker, c.entry_time.isoformat(),
            )
            no_exit += 1
            continue

        gross_pnl = (exit_fill.filled_avg_price - c.entry_price) * c.quantity
        exit_reason = _infer_exit_reason(exit_fill.order_id, c)
        logger.info(
            "Position %d %s qty=%d entry=%.4f exit=%.4f pnl=%+.2f reason=%s%s",
            c.position_id, c.ticker, c.quantity,
            c.entry_price, exit_fill.filled_avg_price, gross_pnl, exit_reason,
            " (dry-run)" if dry_run else "",
        )
        if not dry_run:
            insert_backfilled_trade(conn, c, exit_fill)
            inserted += 1

    if not dry_run:
        conn.commit()

    return BackfillReport(
        candidates_found=len(candidates),
        already_backfilled=0,  # excluded at SQL level
        no_exit_found=no_exit,
        inserted=inserted,
        dry_run=dry_run,
    )

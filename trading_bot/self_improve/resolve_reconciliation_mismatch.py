"""Terminal resolution for stale ``reconciliation_mismatch`` trades rows.

The live recovery path (``StateRecovery._close_db_position``) writes a trades
row with ``exit_reason='reconciliation_mismatch'`` and NULL exit data,
expecting the nightly ``alpaca_backfill`` to pair it with the real Alpaca
SELL fill. But the backfill only repairs rows where a matching fill *exists*.
When the reconciler fired because a position was never actually held at the
broker (a phantom duplicate, or an entry that never filled), there is no SELL
fill to find — so the row stays ``exit_price IS NULL`` forever, retried every
night, silently inflating ``total_trades`` and corrupting per-strategy P&L.

This pass closes that gap. For each ``reconciliation_mismatch`` row with NULL
``exit_price`` older than a safety window:

  1. **Try the existing fill-pairing** (``find_exit_fill``). If a fill now
     exists, repair via ``insert_backfilled_trade`` — the same proven path
     the nightly backfill uses. (Belt-and-suspenders: the backfill runs
     first, so this rarely fires, but it keeps the two passes consistent.)
  2. **No fill → classify by the ENTRY order at Alpaca:**
       - entry never filled / no order id / order too old to confirm →
         **VOID**: ``exit_price = entry_price``, P&L = 0,
         ``exit_reason='void_no_fill'``. A phantom round-trip realized no
         P&L, so zeroing is correct and drops it from the win/loss counts.
       - entry **confirmed filled** but no exit fill → do NOT fabricate a
         price. Stamp ``exit_reason='unresolved_exit'`` and surface it to a
         human via the injected ``alert`` callback. Never silently void real
         money.
  3. Rows younger than ``MIN_AGE_DAYS`` are left untouched so the ordinary
     nightly backfill gets first crack. Today's same-day mismatches self-heal
     through the backfill and must never be voided prematurely.

Idempotent: a resolved row's ``exit_reason`` is no longer
``reconciliation_mismatch``, so it drops out of the candidate query on the
next run.

Usage:
    python -m trading_bot.self_improve.resolve_cli --dry-run
    python -m trading_bot.self_improve.resolve_cli
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from trading_bot.constants import ExitReason
from trading_bot.self_improve.alpaca_backfill import (
    ClosedPositionRow,
    ExitFill,
    _parse_iso,
    find_exit_fill,
    insert_backfilled_trade,
)

logger = logging.getLogger(__name__)


# A reconciliation_mismatch row must be at least this many days old before we
# resolve it, giving the ordinary nightly backfill time to pair it with a real
# fill first. Same-day / next-day mismatches (e.g. today's ORB exits that the
# backfill heals at 22:30 UTC) are never touched here — and so are never voided.
MIN_AGE_DAYS: int = 2

# Notes markers so a resolved row is auditable and the candidate query (keyed
# on exit_reason) never re-selects it.
VOID_NOTE: str = "resolve:void_no_fill (no Alpaca fill; entry never confirmed)"
UNRESOLVED_NOTE: str = "resolve:unresolved_exit (entry filled, exit fill missing — needs human)"

# Alert callback: (title, message) -> None. Injected so the module stays
# decoupled from the Notifier and trivially testable.
AlertFn = Callable[[str, str], None]
# Exit-fill finder signature, injectable for tests.
FillFinder = Callable[[object, ClosedPositionRow], Awaitable[ExitFill | None]]
# Entry-filled confirmer signature, injectable for tests.
EntryConfirmer = Callable[[object, str], Awaitable[bool]]


@dataclass(frozen=True)
class StaleRow:
    """A ``reconciliation_mismatch`` trades row with NULL exit, plus the
    Alpaca order ids from its matched ``positions`` row (if any)."""

    trade_id: int
    ticker: str
    strategy_id: str | None
    exchange: str
    currency: str
    quantity: float
    entry_price: float
    entry_time: datetime
    hold_type: str
    phase: int
    alpaca_order_id: str | None
    alpaca_stop_order_id: str | None
    alpaca_target_order_id: str | None
    alpaca_trail_order_id: str | None

    def as_position(self) -> ClosedPositionRow:
        """Adapt to the ``ClosedPositionRow`` shape ``find_exit_fill`` and
        ``insert_backfilled_trade`` expect. ``position_id`` is unused by those
        functions except for the notes marker; the trade id is a stable
        substitute when no position row matched."""
        return ClosedPositionRow(
            position_id=self.trade_id,
            ticker=self.ticker,
            exchange=self.exchange,
            currency=self.currency,
            strategy_id=self.strategy_id or "unknown",
            quantity=self.quantity,
            entry_price=self.entry_price,
            entry_time=self.entry_time,
            hold_type=self.hold_type,
            phase=self.phase,
            alpaca_order_id=self.alpaca_order_id,
            alpaca_stop_order_id=self.alpaca_stop_order_id,
            alpaca_target_order_id=self.alpaca_target_order_id,
            alpaca_trail_order_id=self.alpaca_trail_order_id,
        )


@dataclass(frozen=True)
class ResolveReport:
    """Summary of a single resolve invocation."""

    candidates: int
    repaired: int
    voided: int
    unresolved: int
    skipped_too_young: int
    dry_run: bool


def load_stale_rows(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    min_age_days: int = MIN_AGE_DAYS,
) -> tuple[list[StaleRow], int]:
    """Load ``reconciliation_mismatch`` trades rows with NULL ``exit_price``
    that are at least ``min_age_days`` old.

    Driven off the **trades** table (not ``positions``) so attribution-less
    rows — ``strategy_id='unknown'`` or with no surviving position row — are
    still resolved. The matching ``positions`` row, when one exists, is joined
    in for its Alpaca order ids (needed to confirm the entry fill and infer an
    exit reason). The join is by ``(ticker, entry_time[:19])`` — the same
    19-char prefix the backfill uses, tolerant of microsecond / offset drift.

    Returns ``(rows_old_enough, skipped_too_young)``.
    """
    cur = conn.execute(
        """
        SELECT t.id, t.ticker, t.strategy_id, t.exchange, t.currency,
               t.quantity, t.entry_price, t.entry_time, t.exit_time,
               t.hold_type, t.phase,
               p.alpaca_order_id, p.alpaca_stop_order_id,
               p.alpaca_target_order_id, p.alpaca_trail_order_id
          FROM trades t
          LEFT JOIN positions p
                 ON p.ticker = t.ticker
                AND substr(p.entry_time, 1, 19) = substr(t.entry_time, 1, 19)
         WHERE t.exit_reason = ?
           AND t.exit_price IS NULL
         ORDER BY t.entry_time
        """,
        (ExitReason.RECONCILIATION_MISMATCH.value,),
    )
    rows: list[StaleRow] = []
    skipped_young = 0
    for r in cur.fetchall():
        entry_dt = _parse_iso(r[7])
        if entry_dt is None:
            logger.warning("Trade %d has unparseable entry_time %r — skipping",
                           r[0], r[7])
            continue
        # Age off exit_time (when the reconciler closed it); fall back to
        # entry_time if exit_time is somehow absent.
        ref_dt = _parse_iso(r[8]) or entry_dt
        age_days = (now - ref_dt).total_seconds() / 86400.0
        if age_days < min_age_days:
            skipped_young += 1
            continue
        rows.append(
            StaleRow(
                trade_id=int(r[0]),
                ticker=str(r[1]),
                strategy_id=r[2],
                exchange=str(r[3]) if r[3] is not None else "",
                currency=str(r[4]) if r[4] is not None else "USD",
                # quantity may be fractional (stored as REAL); never int() it.
                quantity=float(r[5]),
                entry_price=float(r[6]),
                entry_time=entry_dt,
                hold_type=str(r[9]) if r[9] is not None else "intraday",
                phase=int(r[10]) if r[10] is not None else 1,
                alpaca_order_id=r[11],
                alpaca_stop_order_id=r[12],
                alpaca_target_order_id=r[13],
                alpaca_trail_order_id=r[14],
            )
        )
    return rows, skipped_young


async def confirm_entry_filled(client, order_id: str) -> bool:
    """Return True only if Alpaca confirms ``order_id`` reached status filled.

    Conservative: any uncertainty (no order id, lookup error, order aged out
    of Alpaca's history, non-filled status) returns False, which routes the
    row to the VOID path. We only protect a row from voiding when we can
    *positively* confirm its entry filled — that is the one case where zeroing
    might discard real P&L.
    """
    if not order_id:
        return False
    try:
        order = await asyncio.to_thread(client.get_order_by_id, order_id)
    except Exception:
        logger.warning("Could not fetch Alpaca entry order %s — treating as "
                       "unconfirmed (will void)", str(order_id)[:8],
                       exc_info=True)
        return False
    status_str = (getattr(getattr(order, "status", None), "value", "") or "").lower()
    if not status_str:
        status_str = str(getattr(order, "status", "")).lower()
    return "filled" in status_str and "partially" not in status_str


def _void_row(conn: sqlite3.Connection, row: StaleRow) -> None:
    """Mark a phantom row as a zero-P&L void. Keeps the row (audit trail);
    sets exit_price = entry_price so it reads as a flat scratch."""
    conn.execute(
        """
        UPDATE trades
           SET exit_price = ?, exit_time = COALESCE(exit_time, ?),
               gross_pnl = 0, net_pnl = 0, pnl_usd = 0,
               exit_reason = ?, notes = ?
         WHERE id = ?
        """,
        (
            row.entry_price,
            row.entry_time.isoformat(),
            ExitReason.VOID_NO_FILL.value,
            VOID_NOTE,
            row.trade_id,
        ),
    )


def _mark_unresolved(conn: sqlite3.Connection, row: StaleRow) -> None:
    """Flag a row whose entry filled but whose exit fill we cannot find.
    exit_price stays NULL (we will not fabricate one); the changed
    exit_reason makes it terminal so it is not re-processed."""
    conn.execute(
        """
        UPDATE trades
           SET exit_reason = ?, notes = ?
         WHERE id = ?
        """,
        (ExitReason.UNRESOLVED_EXIT.value, UNRESOLVED_NOTE, row.trade_id),
    )


async def resolve(
    conn: sqlite3.Connection,
    client,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    min_age_days: int = MIN_AGE_DAYS,
    alert: AlertFn | None = None,
    fill_finder: FillFinder | None = None,
    entry_confirmer: EntryConfirmer | None = None,
) -> ResolveReport:
    """Resolve every stale ``reconciliation_mismatch`` row to a terminal state.

    See the module docstring for the per-row decision tree. ``alert`` (if
    given) is called once at the end with a single batched summary when any
    rows are left ``unresolved_exit`` — never once per row.
    """
    resolved_now = now or datetime.now(tz=timezone.utc)
    finder = fill_finder or find_exit_fill
    confirmer = entry_confirmer or confirm_entry_filled

    candidates, skipped_young = load_stale_rows(
        conn, now=resolved_now, min_age_days=min_age_days,
    )
    logger.info(
        "Found %d stale reconciliation_mismatch row(s) >= %d day(s) old "
        "(%d younger, left for the nightly backfill)",
        len(candidates), min_age_days, skipped_young,
    )

    repaired = 0
    voided = 0
    unresolved_rows: list[StaleRow] = []

    for row in candidates:
        position = row.as_position()
        exit_fill = await finder(client, position)

        if exit_fill is not None:
            gross = (exit_fill.filled_avg_price - row.entry_price) * row.quantity
            logger.info(
                "%sRepair trade %d %s: paired exit @ %.4f pnl=%+.2f",
                "[DRY-RUN] " if dry_run else "",
                row.trade_id, row.ticker, exit_fill.filled_avg_price, gross,
            )
            if not dry_run:
                insert_backfilled_trade(conn, position, exit_fill)
            repaired += 1
            continue

        entry_filled = await confirmer(client, row.alpaca_order_id or "")
        if entry_filled:
            logger.warning(
                "%sUNRESOLVED trade %d %s: entry order %s confirmed filled but "
                "no exit fill found — flagging for human review",
                "[DRY-RUN] " if dry_run else "",
                row.trade_id, row.ticker,
                str(row.alpaca_order_id or "")[:8],
            )
            if not dry_run:
                _mark_unresolved(conn, row)
            unresolved_rows.append(row)
        else:
            logger.info(
                "%sVOID trade %d %s qty=%.4f: no exit fill and entry not "
                "confirmed filled — zeroing P&L",
                "[DRY-RUN] " if dry_run else "",
                row.trade_id, row.ticker, row.quantity,
            )
            if not dry_run:
                _void_row(conn, row)
            voided += 1

    if not dry_run:
        conn.commit()

    if unresolved_rows and alert is not None:
        sample = ", ".join(
            f"{r.ticker}@{r.entry_time.date().isoformat()}"
            for r in unresolved_rows[:8]
        )
        more = "" if len(unresolved_rows) <= 8 else f" (+{len(unresolved_rows) - 8} more)"
        alert(
            "⚠️ Unresolved exits need review",
            f"{len(unresolved_rows)} reconciliation_mismatch row(s) had a "
            f"confirmed entry fill but no exit fill: {sample}{more}. "
            f"These were NOT voided — inspect Alpaca order history.",
        )

    return ResolveReport(
        candidates=len(candidates),
        repaired=repaired,
        voided=voided,
        unresolved=len(unresolved_rows),
        skipped_too_young=skipped_young,
        dry_run=dry_run,
    )

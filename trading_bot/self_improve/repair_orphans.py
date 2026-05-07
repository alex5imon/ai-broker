"""One-shot repair: reconcile DB positions against Alpaca order/position state.

Two classes of drift left over from the stop-attach-response-loss bug
(see PR #64):

1. ``ENTRY_FAILED`` positions whose Alpaca entry order actually filled.
   The bot stamped them ENTRY_FAILED after a spurious stop-attach failure,
   leaving a real position at the broker invisible to the strategy layer.

2. ``strategy_id='unknown'`` rows with status ``POSITION_OPEN`` created by
   ``StateRecovery._reconcile`` when it encountered the broker positions
   the bot didn't know about. They exist in addition to the legitimate
   ``ENTRY_FAILED`` rows for the same ticker / entry time.

This script reads the live broker state, repairs (1), and merges/closes
(2) so the strategy_manager's "don't double up on same ticker" guard
fires correctly tomorrow. It is idempotent: re-running on a clean DB
finds nothing to repair.

Usage:
    python -m trading_bot.self_improve.repair_orphans --dry-run
    python -m trading_bot.self_improve.repair_orphans
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.config import Config
from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.env import resolve_alpaca_env
from trading_bot.log_setup import setup_logging

logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


@dataclass(frozen=True)
class RepairReport:
    """Summary of a single repair invocation."""

    entry_failed_scanned: int
    entry_failed_repaired: int
    entry_failed_marked_closed: int
    unknown_duplicates_scanned: int
    unknown_duplicates_closed: int
    phantom_live_scanned: int
    phantom_live_closed: int
    dry_run: bool


def _load_entry_failed_with_order_id(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """ENTRY_FAILED rows that have an alpaca_order_id — candidates for
    'order actually filled' check.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM positions "
        "WHERE status = ? AND alpaca_order_id IS NOT NULL "
        "AND alpaca_order_id != '' "
        "ORDER BY entry_time",
        (PositionStatus.ENTRY_FAILED.value,),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_unknown_duplicates(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """``strategy_id='unknown'`` rows still in non-terminal status."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM positions "
        "WHERE strategy_id = 'unknown' "
        "AND status NOT IN ('CLOSED', 'ENTRY_FAILED') "
        "ORDER BY entry_time",
        (),
    ).fetchall()
    return [dict(r) for r in rows]


async def _fetch_live_tickers(client) -> set[str]:
    """Fetch the set of tickers currently held at Alpaca (qty != 0).

    Single broker query at the top of the repair, threaded through every
    decision so the script can distinguish "filled and still held" (flip
    to live) from "filled then exited later" (mark CLOSED).
    """
    try:
        positions = await asyncio.to_thread(client.get_all_positions)
    except Exception:
        logger.warning(
            "Could not list Alpaca positions — falling back to "
            "filled-only check (may flip ghost rows to live)",
            exc_info=True,
        )
        return set()
    out: set[str] = set()
    for p in positions:
        try:
            qty = float(getattr(p, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if abs(qty) > 1e-6:
            out.add(str(getattr(p, "symbol", "")))
    return out


async def _check_order_filled(client, alpaca_order_id: str):
    """Return the Alpaca order if it exists and is filled, else None."""
    try:
        order = await asyncio.to_thread(
            client.get_order_by_id, alpaca_order_id,
        )
    except Exception:
        logger.warning(
            "Could not fetch Alpaca order %s", alpaca_order_id, exc_info=True,
        )
        return None
    status_str = (
        getattr(getattr(order, "status", None), "value", "") or ""
    ).lower()
    if status_str != "filled":
        return None
    return order


async def _find_open_stop_for_ticker(
    client, ticker: str, qty: float,
) -> str | None:
    """Look up an open SELL stop for ``ticker`` matching ``qty``.

    Mirrors the helper now in OrderManager._find_existing_stop, kept here
    as a pure-function variant so the repair script doesn't need to spin
    up the full execution stack.
    """
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus

    try:
        request = GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[ticker],
        )
        orders = await asyncio.to_thread(client.get_orders, filter=request)
    except Exception:
        logger.warning(
            "Could not list open orders for %s", ticker, exc_info=True,
        )
        return None

    for o in orders:
        order_type_obj = (
            getattr(o, "order_type", None) or getattr(o, "type", None)
        )
        order_type = (
            getattr(order_type_obj, "value", "") or ""
        ).lower()
        side_obj = getattr(o, "side", None)
        side = (getattr(side_obj, "value", "") or "").lower()
        try:
            order_qty = float(getattr(o, "qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if order_type != "stop" or side != "sell":
            continue
        if abs(order_qty - qty) > 1e-6:
            continue
        return str(o.id)
    return None


async def _repair_entry_failed(
    conn: sqlite3.Connection,
    client,
    live_tickers: set[str],
    *,
    dry_run: bool,
) -> tuple[int, int, int]:
    """Repair ENTRY_FAILED rows.

    Three outcomes per row:

    - Order **filled and ticker is still held** → flip to
      ``STOP_AND_TARGET_ACTIVE`` (adopt matching broker stop) or
      ``POSITION_OPEN`` (no live stop yet — next reconciler tick
      attaches one).
    - Order **filled but ticker is NOT held at Alpaca** → mark
      ``CLOSED`` with a backfill note. The fill happened then the
      position was exited (intraday close, MOO, manual flatten); the
      previous version of this script wrongly flipped these to
      POSITION_OPEN, creating ghost rows.
    - Order **not filled** (canceled/expired/rejected) → leave
      ``ENTRY_FAILED`` (genuine failure).

    Returns ``(scanned, flipped_to_live, marked_closed)``.
    """
    candidates = _load_entry_failed_with_order_id(conn)
    flipped_live = 0
    marked_closed = 0
    now_iso: str = datetime.now(tz=ET).isoformat()

    for pos in candidates:
        oid: str = str(pos["alpaca_order_id"])
        order = await _check_order_filled(client, oid)
        if order is None:
            logger.debug(
                "Position %d (%s) ENTRY_FAILED is a real failure — "
                "Alpaca order %s is not filled.",
                pos["id"], pos["ticker"], oid[:8],
            )
            continue

        try:
            filled_qty: float = float(order.filled_qty or 0)
            filled_avg: float = float(order.filled_avg_price or 0)
        except (TypeError, ValueError):
            logger.warning(
                "Position %d (%s): Alpaca order %s returned malformed fill "
                "data; skipping.", pos["id"], pos["ticker"], oid[:8],
            )
            continue

        ticker = str(pos["ticker"])
        currently_held: bool = ticker in live_tickers

        if not currently_held:
            # Filled then exited later. Mark CLOSED with a note —
            # never flip a ghost to live.
            logger.info(
                "%sMarking position %d (%s) CLOSED — entry order %s "
                "filled @ %.4f but ticker not currently held at Alpaca "
                "(round-tripped before repair)",
                "[DRY-RUN] " if dry_run else "",
                pos["id"], ticker, oid[:8], filled_avg,
            )
            if not dry_run:
                # No notes column on positions — the log line above is
                # the audit trail. The fill price/quantity is preserved
                # so daily-review backfill can still match this position
                # to its exit fill in the trades table.
                with conn:
                    conn.execute(
                        "UPDATE positions SET status = ?, "
                        "entry_price = ?, quantity = ?, "
                        "updated_at = ? "
                        "WHERE id = ?",
                        (
                            PositionStatus.CLOSED.value, filled_avg,
                            filled_qty, now_iso, pos["id"],
                        ),
                    )
            marked_closed += 1
            continue

        stop_id = await _find_open_stop_for_ticker(
            client, ticker, filled_qty,
        )

        new_status: str = (
            PositionStatus.STOP_AND_TARGET_ACTIVE.value
            if stop_id is not None
            else PositionStatus.POSITION_OPEN.value
        )
        logger.info(
            "%sRepairing position %d (%s): ENTRY_FAILED -> %s "
            "(qty=%.6f @ %.4f, stop_id=%s)",
            "[DRY-RUN] " if dry_run else "",
            pos["id"], ticker, new_status,
            filled_qty, filled_avg, stop_id or "none",
        )

        if dry_run:
            flipped_live += 1
            continue

        with conn:
            conn.execute(
                "UPDATE positions SET status = ?, "
                "entry_price = ?, quantity = ?, "
                "alpaca_stop_order_id = COALESCE(?, alpaca_stop_order_id), "
                "updated_at = ? "
                "WHERE id = ?",
                (
                    new_status, filled_avg, filled_qty, stop_id, now_iso,
                    pos["id"],
                ),
            )
        flipped_live += 1

    return len(candidates), flipped_live, marked_closed


def _close_unknown_duplicates(
    conn: sqlite3.Connection,
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Close ``strategy_id='unknown'`` positions if a sibling strategy_id
    row for the same ticker now exists.

    The repair above flipped the ENTRY_FAILED row back to a live status,
    so the ``unknown`` duplicate is now redundant. We don't delete (audit
    trail), we mark it CLOSED so the strategy layer ignores it.
    """
    candidates = _load_unknown_duplicates(conn)
    closed = 0
    now_iso: str = datetime.now(tz=ET).isoformat()

    for unk in candidates:
        # Look for a sibling on the same ticker that's now live (any
        # status other than CLOSED/ENTRY_FAILED, with a real strategy_id).
        sibling = conn.execute(
            "SELECT id, status, strategy_id FROM positions "
            "WHERE ticker = ? AND id != ? "
            "AND strategy_id != 'unknown' AND strategy_id != '' "
            "AND status NOT IN ('CLOSED', 'ENTRY_FAILED') "
            "ORDER BY entry_time DESC LIMIT 1",
            (unk["ticker"], unk["id"]),
        ).fetchone()
        if sibling is None:
            logger.debug(
                "Position %d (%s, unknown): no live sibling — leaving as-is "
                "(may be a genuine broker-side-only position).",
                unk["id"], unk["ticker"],
            )
            continue

        logger.info(
            "%sClosing unknown duplicate position %d (%s) — sibling %d "
            "now live as %s",
            "[DRY-RUN] " if dry_run else "",
            unk["id"], unk["ticker"], sibling[0], sibling[2],
        )
        if dry_run:
            closed += 1
            continue

        with conn:
            conn.execute(
                "UPDATE positions SET status = 'CLOSED', updated_at = ? "
                "WHERE id = ?",
                (now_iso, unk["id"]),
            )
        closed += 1

    return len(candidates), closed


def _close_phantom_live_rows(
    conn: sqlite3.Connection,
    live_tickers: set[str],
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Close DB rows whose status is non-terminal but whose ticker is
    not currently held at Alpaca.

    Catches two failure modes:

    1. Ghost rows left over from a prior repair run that wrongly
       flipped ENTRY_FAILED → POSITION_OPEN without checking live
       holdings (the bug this PR fixes).
    2. Rows that ``StateRecovery._reconcile`` would normally close as
       ``reconciliation_mismatch``, but silently dropped due to its
       ``db_by_ticker[ticker] = row`` overwrite when two non-terminal
       rows share a ticker.

    The bot's reconciler will eventually catch class 1 alone for unique
    tickers; class 2 needs explicit handling because the reconciler
    can't see the second row at all.

    Returns ``(scanned, closed)``.
    """
    rows = conn.execute(
        "SELECT id, ticker, strategy_id, status, entry_time "
        "FROM positions "
        "WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED') "
        "ORDER BY ticker, entry_time"
    ).fetchall()
    scanned = 0
    closed = 0
    now_iso: str = datetime.now(tz=ET).isoformat()

    # Group by ticker so we can decide which row(s) to close. If
    # Alpaca holds the ticker AND multiple DB rows exist, close the
    # older ones — the latest entry is the one matching the broker's
    # actual position.
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_ticker.setdefault(str(r[1]), []).append({
            "id": int(r[0]), "ticker": str(r[1]),
            "strategy_id": r[2], "status": str(r[3]),
            "entry_time": r[4],
        })

    for ticker, group in by_ticker.items():
        scanned += len(group)
        if ticker not in live_tickers:
            # Whole group is phantom — close everything.
            to_close = group
            reason = "ticker not held at Alpaca (filled then exited)"
        elif len(group) > 1:
            # Multiple rows for a held ticker — keep the most recent,
            # close the older ones (likely older fills already exited
            # and replaced by a fresh entry).
            sorted_rows = sorted(group, key=lambda r: r["entry_time"])
            to_close = sorted_rows[:-1]
            keeper = sorted_rows[-1]
            reason = (
                f"older duplicate of position {keeper['id']} "
                "(both share a held ticker; reconciler can only "
                "track one row per ticker)"
            )
        else:
            continue

        for unk in to_close:
            logger.info(
                "%sClosing phantom position %d (%s, %s, status=%s) — %s",
                "[DRY-RUN] " if dry_run else "",
                unk["id"], unk["ticker"],
                unk["strategy_id"] or "<blank>",
                unk["status"], reason,
            )
            if dry_run:
                closed += 1
                continue
            with conn:
                conn.execute(
                    "UPDATE positions SET status = 'CLOSED', "
                    "updated_at = ? WHERE id = ?",
                    (now_iso, unk["id"]),
                )
            closed += 1

    return scanned, closed


async def repair(
    conn: sqlite3.Connection,
    client,
    *,
    dry_run: bool = False,
) -> RepairReport:
    """Run all three repair passes. Order matters:

    1. ``_repair_entry_failed`` — flip live or close based on whether
       the broker actually holds the ticker.
    2. ``_close_unknown_duplicates`` — close ``unknown`` rows whose
       sibling is now live (sibling lookup depends on step 1).
    3. ``_close_phantom_live_rows`` — close any remaining non-terminal
       row whose ticker isn't held at Alpaca, plus older duplicate
       rows for held tickers. Catches ghost rows from prior repair
       runs and the reconciler's silent-drop bug.
    """
    live_tickers = await _fetch_live_tickers(client)
    logger.info("Alpaca currently holds %d ticker(s): %s",
                len(live_tickers),
                ", ".join(sorted(live_tickers)) or "<none>")

    ef_scanned, ef_repaired, ef_closed = await _repair_entry_failed(
        conn, client, live_tickers, dry_run=dry_run,
    )
    unk_scanned, unk_closed = _close_unknown_duplicates(
        conn, dry_run=dry_run,
    )
    phantom_scanned, phantom_closed = _close_phantom_live_rows(
        conn, live_tickers, dry_run=dry_run,
    )
    return RepairReport(
        entry_failed_scanned=ef_scanned,
        entry_failed_repaired=ef_repaired,
        entry_failed_marked_closed=ef_closed,
        unknown_duplicates_scanned=unk_scanned,
        unknown_duplicates_closed=unk_closed,
        phantom_live_scanned=phantom_scanned,
        phantom_live_closed=phantom_closed,
        dry_run=dry_run,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot: repair orphan ENTRY_FAILED positions and "
                    "merge unknown-strategy duplicates.",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", default=None,
                        help="SQLite path (default: config's database.path)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would change; no DB writes.")
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    db_path = args.db or config.db_path
    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    api_key, secret_key, is_paper = resolve_alpaca_env()
    if not api_key or not secret_key:
        logger.error("Alpaca credentials not found.")
        return 2

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=is_paper)
    logger.info("Connected to Alpaca (%s)", "paper" if is_paper else "live")

    conn = sqlite3.connect(db_path)
    try:
        report = await repair(conn, client, dry_run=args.dry_run)
    finally:
        conn.close()

    logger.info(
        "Repair complete: ef_scanned=%d ef_repaired=%d ef_closed=%d "
        "unk_scanned=%d unk_closed=%d "
        "phantom_scanned=%d phantom_closed=%d dry_run=%s",
        report.entry_failed_scanned, report.entry_failed_repaired,
        report.entry_failed_marked_closed,
        report.unknown_duplicates_scanned, report.unknown_duplicates_closed,
        report.phantom_live_scanned, report.phantom_live_closed,
        report.dry_run,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("repair_orphans")
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())

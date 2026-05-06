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


@dataclass
class RepairReport:
    """Summary of a single repair invocation."""

    entry_failed_scanned: int
    entry_failed_repaired: int
    unknown_duplicates_scanned: int
    unknown_duplicates_closed: int
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
    *,
    dry_run: bool,
) -> tuple[int, int]:
    """Flip ENTRY_FAILED → STOP_AND_TARGET_ACTIVE for rows whose Alpaca
    order actually filled, adopting any matching open stop.

    Returns (scanned, repaired).
    """
    candidates = _load_entry_failed_with_order_id(conn)
    repaired = 0
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

        stop_id = await _find_open_stop_for_ticker(
            client, str(pos["ticker"]), filled_qty,
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
            pos["id"], pos["ticker"], new_status,
            filled_qty, filled_avg, stop_id or "none",
        )

        if dry_run:
            repaired += 1
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
        repaired += 1

    return len(candidates), repaired


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


async def repair(
    conn: sqlite3.Connection,
    client,
    *,
    dry_run: bool = False,
) -> RepairReport:
    """Run both repairs. Order matters: fix ENTRY_FAILED first so the
    sibling lookup in step 2 finds the now-live row."""
    ef_scanned, ef_repaired = await _repair_entry_failed(
        conn, client, dry_run=dry_run,
    )
    unk_scanned, unk_closed = _close_unknown_duplicates(
        conn, dry_run=dry_run,
    )
    return RepairReport(
        entry_failed_scanned=ef_scanned,
        entry_failed_repaired=ef_repaired,
        unknown_duplicates_scanned=unk_scanned,
        unknown_duplicates_closed=unk_closed,
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
        "Repair complete: ef_scanned=%d ef_repaired=%d "
        "unk_scanned=%d unk_closed=%d dry_run=%s",
        report.entry_failed_scanned, report.entry_failed_repaired,
        report.unknown_duplicates_scanned, report.unknown_duplicates_closed,
        report.dry_run,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("repair_orphans")
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())

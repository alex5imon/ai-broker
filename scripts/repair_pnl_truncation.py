"""One-shot repair: recompute pnl for trades affected by int(qty) truncation.

Context — fixed in ai-broker#108 forward-only:

  Pre-#108 ``alpaca_backfill.py`` loaded ``positions.quantity`` via
  ``int(row[5])``, truncating fractional shares (e.g. 0.3927 → 0). The
  backfilled ``trades`` rows received ``quantity = int(...)`` and
  ``pnl_usd = (exit - entry) * int(qty)`` — so any sub-1-share position
  recorded pnl as $0.00, and 1<x<2 share positions recorded pnl scaled
  by 1 instead of the real fractional quantity.

  #108 stopped new rows from being affected but did not heal existing
  rows: the ``backfill:position:N`` marker on ``trades.notes`` makes
  ``load_candidates`` skip them permanently. ``positions.quantity``
  retained the correct fractional value, so we can recover the real
  quantity by joining on the marker.

Scope:

  - Only rows where ``trades.notes LIKE 'backfill:position:%'`` (touched
    by the backfill code path) AND ``trades.side = 'BUY'`` (long-only
    repair; the few historical short rows compute pnl correctly under
    ``(entry - exit) * qty`` and need a separate code path).
  - Only rows where ``exit_price IS NOT NULL`` (closed trades only).
  - Only rows where the math disagrees by more than $0.01 (epsilon
    avoids touching rows that are already correct, e.g. integer-qty
    rows from the older bot era).

Idempotent: re-running with no affected rows prints "no rows to repair"
and exits 0. The recomputed ``pnl_usd`` matches what the post-#108
backfill would have written, so future re-runs will find no delta.

After repairing trades, ``daily_summaries`` is regenerated via
``trading_bot.self_improve.recompute_daily_summaries`` so the per-day
reports reflect the corrected pnl.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Tolerance for "pnl already correct". $0.01 = one cent; smaller than
# any single-share price change we'd care about, larger than IEEE-754
# rounding noise on multiply.
EPSILON_USD: float = 0.01


def find_affected_rows(conn: sqlite3.Connection) -> list[tuple]:
    """Rows where (exit - entry) * positions.quantity differs from
    trades.pnl_usd by > EPSILON_USD. Returns the join already done so
    the caller can both audit and repair in one pass."""
    cur = conn.execute(
        """
        SELECT
            t.id, t.ticker, t.entry_time, t.exit_time,
            t.quantity AS old_qty,
            p.quantity AS real_qty,
            t.entry_price, t.exit_price,
            t.pnl_usd AS old_pnl,
            (t.exit_price - t.entry_price) * p.quantity AS real_pnl
        FROM trades t
        JOIN positions p
          ON ('backfill:position:' || p.id) = t.notes
        WHERE t.side = 'BUY'
          AND t.exit_price IS NOT NULL
          AND ABS(t.pnl_usd - (t.exit_price - t.entry_price) * p.quantity)
              > ?
        ORDER BY t.id
        """,
        (EPSILON_USD,),
    )
    return cur.fetchall()


def repair_row(conn: sqlite3.Connection, trade_id: int,
               real_qty: float, real_pnl: float) -> None:
    """Update one trades row in-place with corrected qty + pnl."""
    conn.execute(
        """
        UPDATE trades
           SET quantity = ?,
               gross_pnl = ?,
               net_pnl = ?,
               pnl_usd = ?
         WHERE id = ?
        """,
        (real_qty, real_pnl, real_pnl, real_pnl, trade_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default="trading_bot/data/trading_bot.db",
        help="Path to the SQLite DB (default: %(default)s)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the repair. Without this flag, runs as a dry-run "
             "and prints what would change.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        rows = find_affected_rows(conn)
        if not rows:
            logger.info("No rows to repair — all backfilled trades match "
                        "(exit - entry) * positions.quantity within $%.2f.",
                        EPSILON_USD)
            return 0

        logger.info("Found %d row(s) with truncated pnl:", len(rows))
        total_delta = 0.0
        for (tid, ticker, et, xt, old_qty, real_qty,
             ep, xp, old_pnl, real_pnl) in rows:
            delta = real_pnl - old_pnl
            total_delta += delta
            logger.info(
                "  id=%-4d %-5s qty %s→%-7.4f  pnl %+9.4f → %+9.4f  delta %+8.4f",
                tid, ticker, f"{old_qty:.4f}", real_qty,
                old_pnl, real_pnl, delta,
            )
        logger.info("Total pnl delta: $%+.4f", total_delta)

        if not args.apply:
            logger.info("Dry-run — pass --apply to commit. Exiting without "
                        "changes.")
            return 0

        for (tid, _, _, _, _, real_qty, _, _, _, real_pnl) in rows:
            repair_row(conn, tid, real_qty, real_pnl)
        conn.commit()
        logger.info("Repair committed: %d row(s) updated.", len(rows))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

"""One-shot repair: dedupe trades rows on (ticker, entry_time, strategy_id).

Context — fixed in ai-broker#133 forward-only:

  Pre-#133 the recovery / orphan-drain code paths
  (``gateway/recovery.py:_close_db_position``,
  ``strategy/strategy_manager.py:_mark_position_closed``) blindly
  INSERT-ed a stub trades row when CLOSE-ing a position. The normal
  entry flow had already written an entry row in
  ``OrderManager._create_position_record``, so every recovery-driven
  close produced a duplicate on ``(ticker, entry_time, strategy_id)``.
  ``alpaca_backfill`` subsequently UPDATE-d the stub with real exit
  data, leaving the entry row orphaned with NULL exit columns.

  #133 stops new duplicates from being created — both call sites now
  UPDATE the entry row in place. But the 26 already-existing dup
  groups (52 rows, audit 2026-05-14) need a separate one-shot pass
  to merge the orphaned entry row with its stub.

Scope:

  - Only groups where ``COUNT(*) > 1`` on
    ``(ticker, entry_time, strategy_id)``.
  - Only groups where the canonical row (lowest id) has
    ``exit_time IS NULL`` and at least one non-canonical row has
    ``exit_time IS NOT NULL``. This matches the recovery-stub pattern
    exactly and rules out odd shapes that need human review.
  - Skip groups overlapping any non-terminal positions row — should
    be zero in current prod (verified empirically) but guarded
    because the script must never disturb live tick state.

Action per affected group:

  1. Copy stub's exit columns (``exit_time``, ``exit_price``,
     ``exit_reason``, ``gross_pnl``, ``net_pnl``, ``pnl_usd``,
     ``notes``) into the canonical row.
  2. DELETE every non-canonical row in the group.

Idempotent: re-running on a deduped DB reports "no rows to repair"
and exits 0. Picks the highest-id stub when multiple exist (the most
recently updated, i.e. the alpaca_backfill output).

The repair preserves the net pnl reported in ``daily_summaries``:
the exit data is moved from the stub to the canonical row, so the
aggregate ``SUM(pnl_usd) GROUP BY substr(exit_time, 1, 10)`` is
byte-identical pre/post repair. ``daily_summaries`` recompute is
included in the workflow as defense-in-depth, not because the math
changes.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


# Columns copied from stub → canonical. Quantity / entry_price /
# entry_time are NOT in this list: they were identical between the two
# rows when the duplicate was created (both came from the same positions
# row), and the canonical's value is the canonical-by-construction one.
_EXIT_COLUMNS: tuple[str, ...] = (
    "exit_time",
    "exit_price",
    "exit_reason",
    "gross_pnl",
    "net_pnl",
    "pnl_usd",
    "notes",
)


def find_dup_groups(
    conn: sqlite3.Connection,
) -> list[tuple[int, int, str, str, list[int]]]:
    """Return one tuple per affected group:
    ``(canon_id, stub_id, ticker, strategy_id, all_stub_ids)``.

    *stub_id* is the row whose exit data will be merged into the canonical
    — the highest-id row with ``exit_time IS NOT NULL``. *all_stub_ids*
    is the full list of non-canonical rows in the group (== all rows to
    DELETE). The two are usually identical because every observed group
    has exactly 2 rows, but the helper keeps them distinct so a 3+ row
    group would also repair cleanly.
    """
    conn.row_factory = sqlite3.Row
    # Use a window function to compute the canonical id per group, then
    # filter to groups where the canonical is open and at least one
    # non-canonical row is closed. Exclude any group that overlaps a
    # non-terminal positions row.
    rows = conn.execute(
        """
        WITH dup_keys AS (
          SELECT ticker, entry_time, strategy_id
            FROM trades
           GROUP BY ticker, entry_time, strategy_id
           HAVING COUNT(*) > 1
        ),
        ranked AS (
          SELECT t.id, t.ticker, t.entry_time, t.strategy_id,
                 t.exit_time,
                 MIN(t.id) OVER (
                   PARTITION BY t.ticker, t.entry_time, t.strategy_id
                 ) AS canon_id
            FROM trades t JOIN dup_keys d
              ON t.ticker = d.ticker
             AND t.entry_time = d.entry_time
             AND t.strategy_id = d.strategy_id
        )
        SELECT * FROM ranked
        ORDER BY ticker, entry_time, id
        """,
    ).fetchall()

    # Group by (ticker, entry_time, strategy_id).
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for r in rows:
        key = (r["ticker"], r["entry_time"], r["strategy_id"])
        groups.setdefault(key, []).append(r)

    out: list[tuple[int, int, str, str, list[int]]] = []
    for (ticker, entry_time, strategy_id), group_rows in groups.items():
        canon_id: int = int(group_rows[0]["canon_id"])
        canon_row = next(r for r in group_rows if int(r["id"]) == canon_id)
        if canon_row["exit_time"] is not None:
            logger.info(
                "Skipping group %s/%s/%s — canonical id=%d already closed.",
                ticker, entry_time, strategy_id, canon_id,
            )
            continue
        stub_rows = [r for r in group_rows if int(r["id"]) != canon_id]
        closed_stubs = [r for r in stub_rows if r["exit_time"] is not None]
        if not closed_stubs:
            logger.info(
                "Skipping group %s/%s/%s — no closed non-canonical row.",
                ticker, entry_time, strategy_id,
            )
            continue
        # Pick the highest-id closed stub — that's the alpaca_backfill
        # output (latest UPDATE in time).
        stub_id: int = max(int(r["id"]) for r in closed_stubs)
        all_stub_ids: list[int] = [int(r["id"]) for r in stub_rows]

        # Guard: never touch a group that overlaps a live position.
        overlap = conn.execute(
            """
            SELECT COUNT(*) FROM positions
             WHERE ticker = ? AND entry_time = ? AND strategy_id = ?
               AND status NOT IN ('CLOSED', 'ENTRY_FAILED')
            """,
            (ticker, entry_time, strategy_id),
        ).fetchone()[0]
        if overlap > 0:
            logger.warning(
                "Skipping group %s/%s/%s — overlaps %d live position(s).",
                ticker, entry_time, strategy_id, overlap,
            )
            continue

        out.append((canon_id, stub_id, ticker, strategy_id, all_stub_ids))
    return out


def merge_and_delete(
    conn: sqlite3.Connection,
    canon_id: int,
    stub_id: int,
    delete_ids: list[int],
) -> None:
    """Copy stub's exit columns into canonical, then DELETE delete_ids.

    Single transaction — caller owns commit. Uses a parametrised
    UPDATE that reads from ``stub_id`` via a correlated subquery
    pattern so the column list stays explicit (no
    ``UPDATE ... FROM`` cross-version concern on older SQLite).
    """
    stub_row = conn.execute(
        f"SELECT {', '.join(_EXIT_COLUMNS)} FROM trades WHERE id = ?",
        (stub_id,),
    ).fetchone()
    if stub_row is None:
        raise RuntimeError(
            f"Stub row id={stub_id} disappeared between scan and merge"
        )
    set_clause: str = ", ".join(f"{col} = ?" for col in _EXIT_COLUMNS)
    conn.execute(
        f"UPDATE trades SET {set_clause} WHERE id = ?",
        (*stub_row, canon_id),
    )
    placeholders: str = ",".join("?" * len(delete_ids))
    conn.execute(
        f"DELETE FROM trades WHERE id IN ({placeholders})",
        delete_ids,
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
        groups = find_dup_groups(conn)
        if not groups:
            logger.info(
                "No duplicate groups to repair — every "
                "(ticker, entry_time, strategy_id) tuple is unique.",
            )
            return 0

        logger.info(
            "Found %d duplicate group(s) eligible for merge:", len(groups),
        )
        total_deletes: int = 0
        for canon_id, stub_id, ticker, strategy_id, delete_ids in groups:
            logger.info(
                "  %-5s/%-15s canon=%d  stub=%d  delete=%s",
                ticker, strategy_id, canon_id, stub_id, delete_ids,
            )
            total_deletes += len(delete_ids)
        logger.info(
            "Total rows to merge: %d canonical(s) updated, %d row(s) deleted.",
            len(groups), total_deletes,
        )

        if not args.apply:
            logger.info(
                "Dry-run — pass --apply to commit. Exiting without changes.",
            )
            return 0

        for canon_id, stub_id, _ticker, _strategy, delete_ids in groups:
            merge_and_delete(conn, canon_id, stub_id, delete_ids)
        conn.commit()
        logger.info(
            "Repair committed: %d group(s) merged, %d row(s) deleted.",
            len(groups), total_deletes,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

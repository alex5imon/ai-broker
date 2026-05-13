"""Smoke tests for the one-shot scripts/repair_pnl_truncation.py.

These cover the three behaviours that matter for the live run:

1. ``find_affected_rows`` joins on positions.id correctly and excludes
   rows where the math is already within tolerance.
2. ``repair_row`` overwrites quantity + pnl in place.
3. ``main`` is idempotent — re-running with no truncated rows reports
   "no rows to repair" and exits 0 without writing.

The script will be deleted in a follow-up PR after the live run.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

# Load the script under test by path so we don't need to mark the
# top-level ``scripts/`` directory as a package.
_SPEC = importlib.util.spec_from_file_location(
    "repair_pnl_truncation",
    Path(__file__).resolve().parents[2] / "scripts" / "repair_pnl_truncation.py",
)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["repair_pnl_truncation"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

find_affected_rows = _MODULE.find_affected_rows
main = _MODULE.main
repair_row = _MODULE.repair_row


def _seed(conn: sqlite3.Connection, *,
          position_id: int, real_qty: float,
          trade_id: int, trade_qty: float,
          entry: float, exit_: float, pnl: float) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            id, ticker, exchange, currency, quantity, entry_price,
            entry_time, status, hold_type, phase, strategy_id
        ) VALUES (?, 'SPY', 'NYSE', 'USD', ?, ?,
                  '2026-04-01T15:45:00-04:00', 'CLOSED',
                  'swing', 1, 'overnight_drift')
        """,
        (position_id, real_qty, entry),
    )
    conn.execute(
        """
        INSERT INTO trades (
            id, ticker, exchange, currency, side,
            entry_time, entry_price, quantity,
            exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, pnl_usd,
            hold_type, phase, strategy_id, notes
        ) VALUES (?, 'SPY', 'NYSE', 'USD', 'BUY',
                  '2026-04-01T15:45:00-04:00', ?, ?,
                  '2026-04-02T09:31:00-04:00', ?, 'manual',
                  ?, ?, ?,
                  'swing', 1, 'overnight_drift', ?)
        """,
        (trade_id, entry, trade_qty, exit_, pnl, pnl, pnl,
         f"backfill:position:{position_id}"),
    )
    conn.commit()


@pytest.mark.unit
def test_find_affected_rows_flags_truncated_pnl(tmp_db):
    # positions stores 0.3927 (real qty); trades was written with
    # int(0.3927)=0 and pnl=0. Real pnl = (98 - 100) * 0.3927 = -0.7854.
    _seed(tmp_db,
          position_id=1, real_qty=0.3927,
          trade_id=10, trade_qty=0, entry=100.0, exit_=98.0, pnl=0.0)
    rows = find_affected_rows(tmp_db)
    assert len(rows) == 1
    # Schema: id, ticker, entry_time, exit_time, old_qty, real_qty,
    # entry_price, exit_price, old_pnl, real_pnl
    assert rows[0][0] == 10
    assert rows[0][5] == pytest.approx(0.3927)
    assert rows[0][9] == pytest.approx((98.0 - 100.0) * 0.3927)


@pytest.mark.unit
def test_find_affected_rows_skips_already_correct(tmp_db):
    # Real qty = trade qty = 5, pnl = (98-100)*5 = -10. Match.
    _seed(tmp_db,
          position_id=1, real_qty=5.0,
          trade_id=10, trade_qty=5.0, entry=100.0, exit_=98.0, pnl=-10.0)
    assert find_affected_rows(tmp_db) == []


@pytest.mark.unit
def test_main_dry_run_writes_nothing(tmp_db, tmp_path):
    _seed(tmp_db,
          position_id=1, real_qty=0.3927,
          trade_id=10, trade_qty=0, entry=100.0, exit_=98.0, pnl=0.0)
    # main() opens its own connection — close ours and write to the
    # same file so the script can find it.
    db_path = tmp_path / "smoke.db"
    backup = sqlite3.connect(db_path)
    tmp_db.backup(backup)
    backup.close()

    exit_code = main(["--db", str(db_path)])
    assert exit_code == 0

    # Re-open and confirm row is untouched.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT quantity, pnl_usd FROM trades WHERE id = 10"
    ).fetchone()
    conn.close()
    assert row == (0, 0.0), "dry-run must not modify rows"


@pytest.mark.unit
def test_main_apply_repairs_and_is_idempotent(tmp_db, tmp_path):
    _seed(tmp_db,
          position_id=1, real_qty=0.3927,
          trade_id=10, trade_qty=0, entry=100.0, exit_=98.0, pnl=0.0)
    db_path = tmp_path / "apply.db"
    backup = sqlite3.connect(db_path)
    tmp_db.backup(backup)
    backup.close()

    # First run: writes.
    assert main(["--db", str(db_path), "--apply"]) == 0

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT quantity, pnl_usd FROM trades WHERE id = 10"
    ).fetchone()
    expected_pnl = (98.0 - 100.0) * 0.3927
    assert row[0] == pytest.approx(0.3927)
    assert row[1] == pytest.approx(expected_pnl)

    # Second run: idempotent — nothing to repair.
    assert main(["--db", str(db_path), "--apply"]) == 0
    # Row still matches.
    row = conn.execute(
        "SELECT quantity, pnl_usd FROM trades WHERE id = 10"
    ).fetchone()
    conn.close()
    assert row[0] == pytest.approx(0.3927)
    assert row[1] == pytest.approx(expected_pnl)


@pytest.mark.unit
def test_main_missing_db_returns_2(tmp_path):
    assert main(["--db", str(tmp_path / "does-not-exist.db")]) == 2

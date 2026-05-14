"""Tests for ``scripts/repair_trades_dedupe.py``.

Pre-#133 the recovery / orphan-drain paths produced duplicate trades rows
on ``(ticker, entry_time, strategy_id)``: the canonical entry row (lowest
id, NULL exit columns) plus a stub created when the position closed.
This script merges the stub's exit data into the canonical row and
deletes the stub.

Coverage:

- ``find_dup_groups`` only flags groups where canonical is open and at
  least one non-canonical row is closed (the recovery-stub pattern).
- Groups overlapping a live position are skipped.
- Groups with multiple stub rows pick the highest-id (most recent) one
  and delete all non-canonical rows.
- ``merge_and_delete`` copies the exit columns and deletes the stubs in
  a single transaction.
- ``main`` dry-run / apply / idempotent re-run.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Make scripts/ importable for direct unit-testing.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import repair_trades_dedupe as repair  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "trading_bot.db"
    c = sqlite3.connect(db_path)
    c.executescript(
        """
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            exchange TEXT NOT NULL,
            currency TEXT NOT NULL,
            quantity REAL NOT NULL,
            entry_price REAL NOT NULL,
            entry_time TEXT NOT NULL,
            status TEXT NOT NULL,
            hold_type TEXT NOT NULL,
            phase INTEGER NOT NULL,
            strategy_id TEXT
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL,
            exchange TEXT NOT NULL,
            currency TEXT NOT NULL,
            side TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            entry_price REAL NOT NULL,
            quantity REAL NOT NULL,
            exit_time TEXT,
            exit_price REAL,
            exit_reason TEXT,
            gross_pnl REAL,
            net_pnl REAL,
            pnl_usd REAL,
            hold_type TEXT NOT NULL,
            phase INTEGER NOT NULL,
            strategy_id TEXT,
            notes TEXT
        );
        """,
    )
    c.commit()
    return c


def _insert_entry_row(
    conn: sqlite3.Connection,
    *,
    ticker: str = "SPY",
    entry_time: str = "2026-05-04T15:45:37.982852-04:00",
    strategy_id: str = "overnight_drift",
    quantity: float = 0.4412,
) -> int:
    cur = conn.execute(
        "INSERT INTO trades "
        "(ticker, exchange, currency, side, entry_time, entry_price, "
        " quantity, hold_type, phase, strategy_id) "
        "VALUES (?, 'US', 'USD', 'BUY', ?, 100.0, ?, 'intraday', 1, ?)",
        (ticker, entry_time, quantity, strategy_id),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


def _insert_stub_row(
    conn: sqlite3.Connection,
    *,
    ticker: str = "SPY",
    entry_time: str = "2026-05-04T15:45:37.982852-04:00",
    strategy_id: str = "overnight_drift",
    quantity: float = 0.4412,
    exit_time: str = "2026-05-05T09:30:00-04:00",
    exit_price: float | None = 101.5,
    pnl: float | None = 0.66,
    exit_reason: str = "manual",
    notes: str = "backfill:position:42",
) -> int:
    cur = conn.execute(
        "INSERT INTO trades "
        "(ticker, exchange, currency, side, entry_time, entry_price, "
        " quantity, exit_time, exit_price, exit_reason, "
        " gross_pnl, net_pnl, pnl_usd, "
        " hold_type, phase, strategy_id, notes) "
        "VALUES (?, 'US', 'USD', 'BUY', ?, 100.0, ?, "
        "        ?, ?, ?, ?, ?, ?, 'intraday', 1, ?, ?)",
        (
            ticker, entry_time, quantity,
            exit_time, exit_price, exit_reason,
            pnl, pnl, pnl,
            strategy_id, notes,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# find_dup_groups
# ---------------------------------------------------------------------------


def test_find_returns_empty_when_no_dups(conn: sqlite3.Connection) -> None:
    _insert_entry_row(conn)
    assert repair.find_dup_groups(conn) == []


def test_find_flags_canonical_open_stub_closed(conn: sqlite3.Connection) -> None:
    canon = _insert_entry_row(conn)
    stub = _insert_stub_row(conn)

    groups = repair.find_dup_groups(conn)
    assert len(groups) == 1
    canon_id, stub_id, ticker, strategy_id, delete_ids = groups[0]
    assert canon_id == canon
    assert stub_id == stub
    assert ticker == "SPY"
    assert strategy_id == "overnight_drift"
    assert delete_ids == [stub]


def test_find_skips_when_canonical_already_closed(
    conn: sqlite3.Connection,
) -> None:
    """If canonical was already closed (e.g., by a prior repair run or
    real exit path), don't touch the group — that could clobber real
    data with stub data.
    """
    canon = _insert_entry_row(conn)
    conn.execute(
        "UPDATE trades SET exit_time = ?, exit_reason = ?, pnl_usd = ? "
        "WHERE id = ?",
        ("2026-05-05T09:30:00-04:00", "stop_loss", -0.22, canon),
    )
    _insert_stub_row(conn)
    conn.commit()
    assert repair.find_dup_groups(conn) == []


def test_find_skips_when_no_closed_stub(conn: sqlite3.Connection) -> None:
    """Two open rows for the same key shouldn't happen but if they do,
    there's no exit data to merge — skip rather than guess."""
    _insert_entry_row(conn)
    _insert_entry_row(conn)  # second open row at same key
    assert repair.find_dup_groups(conn) == []


def test_find_skips_when_overlaps_live_position(
    conn: sqlite3.Connection,
) -> None:
    """Live position with status POSITION_OPEN must never be touched."""
    _insert_entry_row(conn)
    _insert_stub_row(conn)
    conn.execute(
        "INSERT INTO positions "
        "(ticker, exchange, currency, quantity, entry_price, entry_time, "
        " status, hold_type, phase, strategy_id) "
        "VALUES ('SPY', 'US', 'USD', 0.4412, 100.0, "
        " '2026-05-04T15:45:37.982852-04:00', 'POSITION_OPEN', 'intraday', "
        " 1, 'overnight_drift')",
    )
    conn.commit()
    assert repair.find_dup_groups(conn) == []


def test_find_handles_three_row_group(conn: sqlite3.Connection) -> None:
    """Picks the highest-id closed stub as the merge source and queues
    every non-canonical row for deletion."""
    canon = _insert_entry_row(conn)
    stub_a = _insert_stub_row(conn, exit_time="2026-05-05T09:30:00-04:00")
    stub_b = _insert_stub_row(conn, exit_time="2026-05-05T10:00:00-04:00",
                              exit_price=101.6, pnl=0.71)

    groups = repair.find_dup_groups(conn)
    assert len(groups) == 1
    canon_id, stub_id, _, _, delete_ids = groups[0]
    assert canon_id == canon
    assert stub_id == stub_b  # highest-id closed stub
    assert sorted(delete_ids) == sorted([stub_a, stub_b])


# ---------------------------------------------------------------------------
# merge_and_delete
# ---------------------------------------------------------------------------


def test_merge_copies_exit_columns_and_deletes_stub(
    conn: sqlite3.Connection,
) -> None:
    canon = _insert_entry_row(conn)
    stub = _insert_stub_row(
        conn,
        exit_time="2026-05-05T09:30:00-04:00",
        exit_price=101.5,
        pnl=0.66,
        exit_reason="manual",
        notes="backfill:position:42",
    )

    repair.merge_and_delete(conn, canon, stub, [stub])
    conn.commit()

    canon_row = conn.execute(
        "SELECT exit_time, exit_price, exit_reason, pnl_usd, notes, quantity "
        "FROM trades WHERE id = ?",
        (canon,),
    ).fetchone()
    assert canon_row is not None
    assert canon_row[0] == "2026-05-05T09:30:00-04:00"
    assert abs(canon_row[1] - 101.5) < 1e-9
    assert canon_row[2] == "manual"
    assert abs(canon_row[3] - 0.66) < 1e-9
    assert canon_row[4] == "backfill:position:42"
    # Quantity must NOT be overwritten — canonical kept its entry-row qty.
    assert abs(canon_row[5] - 0.4412) < 1e-9

    # Stub is gone.
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE id = ?", (stub,),
    ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# main — dry-run / apply / idempotent
# ---------------------------------------------------------------------------


def test_main_dry_run_does_not_mutate(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    _insert_entry_row(conn)
    _insert_stub_row(conn)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()

    rc = repair.main(["--db", str(db_path)])
    assert rc == 0

    after = sqlite3.connect(db_path)
    try:
        assert after.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 2
    finally:
        after.close()


def test_main_apply_merges_and_deletes(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    canon = _insert_entry_row(conn)
    _insert_stub_row(conn, exit_price=99.0, pnl=-0.44)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()

    rc = repair.main(["--db", str(db_path), "--apply"])
    assert rc == 0

    after = sqlite3.connect(db_path)
    try:
        # One row left, exit data populated, id preserved.
        rows = after.execute(
            "SELECT id, exit_price, pnl_usd FROM trades"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == canon
        assert abs(rows[0][1] - 99.0) < 1e-9
        assert abs(rows[0][2] - (-0.44)) < 1e-9
        # Re-running is a no-op.
        rc2 = repair.main(["--db", str(db_path), "--apply"])
        assert rc2 == 0
        assert after.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    finally:
        after.close()


def test_main_preserves_total_pnl_across_repair(
    conn: sqlite3.Connection, tmp_path: Path,
) -> None:
    """Aggregate ``SUM(pnl_usd)`` across the trades table is unchanged
    pre/post repair — the exit data is moved, not invented or dropped.
    Pinning this invariant protects ``daily_summaries`` from accidental
    drift.
    """
    _insert_entry_row(conn, ticker="SPY")
    _insert_stub_row(conn, ticker="SPY", pnl=0.12)
    _insert_entry_row(conn, ticker="XLF", entry_time="2026-05-05T15:45:00-04:00")
    _insert_stub_row(
        conn, ticker="XLF",
        entry_time="2026-05-05T15:45:00-04:00",
        pnl=-0.05,
    )
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    conn.close()

    before_conn = sqlite3.connect(db_path)
    try:
        before = before_conn.execute(
            "SELECT ROUND(SUM(pnl_usd), 4) FROM trades"
        ).fetchone()[0]
    finally:
        before_conn.close()

    rc = repair.main(["--db", str(db_path), "--apply"])
    assert rc == 0

    after_conn = sqlite3.connect(db_path)
    try:
        after = after_conn.execute(
            "SELECT ROUND(SUM(pnl_usd), 4) FROM trades"
        ).fetchone()[0]
    finally:
        after_conn.close()

    assert before == after


def test_main_db_missing_returns_nonzero(tmp_path: Path) -> None:
    rc = repair.main(["--db", str(tmp_path / "nope.db")])
    assert rc == 2

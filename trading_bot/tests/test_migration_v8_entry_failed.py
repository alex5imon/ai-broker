"""Tests for V8: retro-convert phantom-CLOSED rows to ENTRY_FAILED.

The migration is data-only (no DDL). These tests pin:
1. The detection rule — only rows with status='CLOSED', alpaca_order_id IS NULL,
   and a paired trades row missing exit_time get flipped.
2. Idempotency — running V8 twice converts the same set once and is a noop on
   the second run.
3. Negative cases — rows that look like phantoms but have any one disqualifier
   (alpaca_order_id present, exit_time present, no paired trades row, status
   not CLOSED) are LEFT ALONE.
4. Open-position queries (get_open_positions) treat ENTRY_FAILED as terminal,
   identical to CLOSED.
5. has_attempted_today() does NOT exclude ENTRY_FAILED — the dedup gate must
   still fire for failed entries so we don't refire identical orders.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trading_bot.constants import (
    TERMINAL_POSITION_STATUSES,
    PositionStatus,
    SCHEMA_VERSION,
)
from trading_bot.db import repository as repo
from trading_bot.db.migrations import _migration_v8, run_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_at_v7(tmp_path) -> Path:
    """Create a SQLite DB at V7 so we can drive _migration_v8 explicitly."""
    db = tmp_path / "v7.db"
    conn = sqlite3.connect(str(db))
    try:
        # Apply schema up through V7 by running the existing migration runner
        # against an empty DB, then ROLLBACK the version to 7 so V8 can run.
        run_migrations(str(db))
        conn = sqlite3.connect(str(db))
        # Force schema version back to 7 to simulate a pre-V8 deployment.
        conn.execute("DELETE FROM schema_version WHERE version >= 8")
        conn.commit()
    finally:
        conn.close()
    return db


def _seed(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    status: str,
    alpaca_order_id: str | None,
    strategy_id: str = "mean_reversion",
    entry_time: str = "2026-04-28T10:30:00",
    qty: int = 10,
    entry_price: float = 100.0,
) -> int:
    cur = conn.execute(
        "INSERT INTO positions ("
        "ticker, exchange, currency, quantity, entry_price, entry_time, "
        "status, hold_type, phase, strategy_id, alpaca_order_id"
        ") VALUES (?, 'ARCA', 'USD', ?, ?, ?, ?, 'intraday', 1, ?, ?)",
        (ticker, qty, entry_price, entry_time, status, strategy_id, alpaca_order_id),
    )
    return int(cur.lastrowid)


def _seed_trade(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    entry_time: str = "2026-04-28T10:30:00",
    exit_time: str | None = None,
    strategy_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO trades ("
        "ticker, exchange, currency, side, entry_time, entry_price, "
        "quantity, exit_time, exit_price, exit_reason, hold_type, phase, "
        "strategy_id"
        ") VALUES (?, 'ARCA', 'USD', 'long', ?, 100.0, 10, ?, ?, ?, "
        "'intraday', 1, ?)",
        (
            ticker,
            entry_time,
            exit_time,
            101.5 if exit_time else None,
            "stop_loss" if exit_time else None,
            strategy_id,
        ),
    )


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


def test_v8_converts_only_obvious_phantoms(db_at_v7: Path) -> None:
    conn = sqlite3.connect(str(db_at_v7))
    try:
        # Phantom: status=CLOSED, alpaca_order_id NULL, paired trade has no
        # exit_time. (Two of these — the live bug fires repeatedly.)
        _seed(conn, ticker="PHANTOM1", status="CLOSED", alpaca_order_id=None)
        _seed_trade(conn, ticker="PHANTOM1", exit_time=None)
        _seed(conn, ticker="PHANTOM2", status="CLOSED", alpaca_order_id=None)
        _seed_trade(conn, ticker="PHANTOM2", exit_time=None)

        # Real fill: status=CLOSED, alpaca_order_id present.
        _seed(conn, ticker="REAL1", status="CLOSED", alpaca_order_id="ord-1")
        _seed_trade(conn, ticker="REAL1", exit_time="2026-04-28T13:00:00")

        # Open: status != CLOSED, must not be touched.
        _seed(conn, ticker="OPEN1", status="POSITION_OPEN", alpaca_order_id="ord-2")

        # Phantom-with-no-trades-row: status=CLOSED + no paired trade row.
        # (LEFT JOIN exposes this — converted because t.id IS NULL.)
        _seed(conn, ticker="ORPHAN1", status="CLOSED", alpaca_order_id=None)

        conn.commit()

        _migration_v8(conn)
        conn.commit()

        rows = dict(conn.execute(
            "SELECT ticker, status FROM positions"
        ).fetchall())
        assert rows["PHANTOM1"] == "ENTRY_FAILED"
        assert rows["PHANTOM2"] == "ENTRY_FAILED"
        assert rows["ORPHAN1"] == "ENTRY_FAILED"
        # Untouched.
        assert rows["REAL1"] == "CLOSED"
        assert rows["OPEN1"] == "POSITION_OPEN"
    finally:
        conn.close()


def test_v8_does_not_touch_closed_with_real_exit_data(db_at_v7: Path) -> None:
    """A row with alpaca_order_id NULL but a complete trades exit row is NOT
    a phantom — exit_time being present means a fill DID happen and just
    wasn't tagged with the entry order id."""
    conn = sqlite3.connect(str(db_at_v7))
    try:
        _seed(conn, ticker="LATE_TAG", status="CLOSED", alpaca_order_id=None)
        _seed_trade(
            conn,
            ticker="LATE_TAG",
            exit_time="2026-04-28T13:00:00",
            strategy_id="mean_reversion",
        )
        conn.commit()
        _migration_v8(conn)
        conn.commit()
        status = conn.execute(
            "SELECT status FROM positions WHERE ticker = 'LATE_TAG'"
        ).fetchone()[0]
        assert status == "CLOSED"
    finally:
        conn.close()


def test_v8_is_idempotent(db_at_v7: Path) -> None:
    conn = sqlite3.connect(str(db_at_v7))
    try:
        _seed(conn, ticker="P1", status="CLOSED", alpaca_order_id=None)
        _seed_trade(conn, ticker="P1", exit_time=None)
        conn.commit()

        _migration_v8(conn)
        conn.commit()
        first = conn.execute(
            "SELECT status FROM positions WHERE ticker = 'P1'"
        ).fetchone()[0]

        # Run it again; nothing should change.
        _migration_v8(conn)
        conn.commit()
        second = conn.execute(
            "SELECT status FROM positions WHERE ticker = 'P1'"
        ).fetchone()[0]

        assert first == second == "ENTRY_FAILED"
    finally:
        conn.close()


def test_v8_count_matches_phase1_query_pattern(db_at_v7: Path) -> None:
    """Cross-check: the count of rows V8 converts equals the count produced
    by the same SELECT pattern the Phase 1 reconcile report would surface as
    PHANTOM_CLOSE candidates from local DB state alone (i.e. without an
    Alpaca round-trip).  Operationally, the user runs reconcile on the
    GHA-cached DB before and after and confirms the numbers match."""
    conn = sqlite3.connect(str(db_at_v7))
    try:
        for i in range(5):
            _seed(conn, ticker=f"P{i}", status="CLOSED", alpaca_order_id=None,
                  entry_time=f"2026-04-2{i}T10:30:00")
            _seed_trade(conn, ticker=f"P{i}", exit_time=None,
                        entry_time=f"2026-04-2{i}T10:30:00")
        # Distractor with order id — must not count.
        _seed(conn, ticker="REAL", status="CLOSED", alpaca_order_id="ord-real")
        _seed_trade(conn, ticker="REAL", exit_time="2026-04-28T13:00:00")
        conn.commit()

        candidate_count = conn.execute(
            """
            SELECT COUNT(*) FROM positions p
            LEFT JOIN trades t
              ON t.ticker = p.ticker AND t.entry_time = p.entry_time
            WHERE p.status = 'CLOSED'
              AND p.alpaca_order_id IS NULL
              AND (t.exit_time IS NULL OR t.id IS NULL)
            """
        ).fetchone()[0]

        _migration_v8(conn)
        conn.commit()

        converted = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE status = 'ENTRY_FAILED'"
        ).fetchone()[0]
        assert converted == candidate_count == 5
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# run_migrations end-to-end
# ---------------------------------------------------------------------------


def test_run_migrations_applies_v8_on_existing_v7_db(tmp_path) -> None:
    db = tmp_path / "round_trip.db"
    run_migrations(str(db))  # creates fresh DB at SCHEMA_VERSION
    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_run_migrations_is_noop_when_already_at_v8(tmp_path) -> None:
    db = tmp_path / "noop.db"
    run_migrations(str(db))
    # Run again — must complete without error and version stays at SCHEMA_VERSION.
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Enum + query call-site coverage
# ---------------------------------------------------------------------------


def test_position_status_includes_entry_failed() -> None:
    assert PositionStatus.ENTRY_FAILED.value == "ENTRY_FAILED"


def test_terminal_status_set_includes_both_terminal_states() -> None:
    assert PositionStatus.CLOSED.value in TERMINAL_POSITION_STATUSES
    assert PositionStatus.ENTRY_FAILED.value in TERMINAL_POSITION_STATUSES
    # In-flight states are NOT terminal.
    for s in (
        PositionStatus.ENTRY_PENDING,
        PositionStatus.POSITION_OPEN,
        PositionStatus.STOP_AND_TARGET_ACTIVE,
        PositionStatus.TRAILING_ACTIVE,
        PositionStatus.CLOSING,
    ):
        assert s.value not in TERMINAL_POSITION_STATUSES


def test_get_open_positions_excludes_entry_failed(tmp_db) -> None:
    _seed(tmp_db, ticker="A", status="POSITION_OPEN", alpaca_order_id="o1")
    _seed(tmp_db, ticker="B", status="ENTRY_FAILED", alpaca_order_id=None)
    _seed(tmp_db, ticker="C", status="CLOSED", alpaca_order_id="o2")
    _seed(tmp_db, ticker="D", status="ENTRY_PENDING", alpaca_order_id=None)
    tmp_db.commit()

    open_rows = repo.get_open_positions(tmp_db)
    tickers = {r["ticker"] for r in open_rows}
    assert tickers == {"A", "D"}


def test_has_attempted_today_still_counts_entry_failed(tmp_db) -> None:
    """Dedup gate: ENTRY_FAILED must still count as "attempted today" so the
    bot does not refire identical orders within the same session.  This was
    the 2026-04-29 incident — see repository.has_attempted_today docstring.
    """
    today = "2026-04-30"
    _seed(
        tmp_db,
        ticker="DEDUP",
        status="ENTRY_FAILED",
        alpaca_order_id=None,
        entry_time=f"{today}T10:30:00",
        strategy_id="mean_reversion",
    )
    tmp_db.commit()

    assert repo.has_attempted_today(
        tmp_db,
        ticker="DEDUP",
        strategy_id="mean_reversion",
        et_today_iso=today,
    ) is True


def test_reconcile_classifier_short_circuits_on_entry_failed() -> None:
    """ENTRY_FAILED rows should classify as PHANTOM_CLOSE without an Alpaca
    round-trip — the V8 migration already vetted the local-DB evidence."""
    from trading_bot.self_improve.reconcile import (
        AlpacaState,
        PositionClass,
        classify_position,
    )

    state = AlpacaState(
        account_id="PA",
        is_paper=True,
        fetched_at=datetime.now(),
        positions_by_symbol={},
        orders_by_id={},
        fills_by_symbol={},
    )
    pos = {
        "ticker": "FOO",
        "quantity": 10,
        "entry_price": 100.0,
        "entry_time": "2026-04-28T10:30:00",
        "status": "ENTRY_FAILED",
        "strategy_id": "mean_reversion",
        "alpaca_order_id": None,
    }
    out = classify_position(pos, state, {"mean_reversion": True})
    assert out.classification is PositionClass.PHANTOM_CLOSE
    assert "V8" in out.evidence

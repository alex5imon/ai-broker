"""Tests for V11: add ``positions.alpaca_exit_order_id`` column.

Pre-V11 the strategy-driven exit order id lived only in
``OrderManager._ActiveOrder.alpaca_exit_order_id`` (in-memory). Every
overnight_drift exit straddled a process boundary, so the next tick
re-fired the same exit and relied on Alpaca's ``held_for_orders``
guard plus StateRecovery to reconcile. See
memory/overnight_drift_exit_orphans.md.

V11 adds a nullable TEXT column so the order id survives across ticks.
These tests pin:

1. Fresh DB at SCHEMA_VERSION has the column.
2. Pre-V11 DB upgraded by ``run_migrations`` ends up with the column,
   existing rows backfilled to NULL.
3. The migration is idempotent — running V11 twice is a no-op the
   second time.
4. The column is part of ``positions.SELECT *`` so VirtualPortfolio
   and OrderManager hydration see it without further code changes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.migrations import _migration_v11, run_migrations


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def db_at_v10(tmp_path: Path) -> Path:
    """Build a SQLite DB at V10 so we can drive _migration_v11 directly."""
    db = tmp_path / "v10.db"
    # Apply all migrations then rewind the version pointer to 10. The
    # full schema already includes the V11 column when SCHEMA_VERSION=11,
    # so to simulate "DB last migrated at V10" we both rewind the
    # schema_version row AND drop the column.
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM schema_version WHERE version >= 11")
        # Drop the V11 column to mirror a real V10-era schema.
        if "alpaca_exit_order_id" in _column_names(conn, "positions"):
            conn.execute("ALTER TABLE positions DROP COLUMN alpaca_exit_order_id")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.unit
def test_fresh_db_at_target_version_has_column(tmp_path: Path) -> None:
    """A clean run_migrations on an empty DB lands at SCHEMA_VERSION
    with the column already present (via _SCHEMA_SQL on V4)."""
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        cols = _column_names(conn, "positions")
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "alpaca_exit_order_id" in cols
    assert version == SCHEMA_VERSION


@pytest.mark.unit
def test_v11_adds_column_on_existing_db(db_at_v10: Path) -> None:
    """V10 → V11 adds the new column; existing rows survive intact."""
    conn = sqlite3.connect(str(db_at_v10))
    try:
        # Seed an existing position row to prove backfill is non-destructive.
        conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, quantity, "
            "entry_price, entry_time, status, hold_type, phase, strategy_id) "
            "VALUES ('SPY','NYSE','USD',10,100.0,'2026-05-01T10:00:00-04:00',"
            "'POSITION_OPEN','intraday',1,'mean_reversion')"
        )
        conn.commit()
        assert "alpaca_exit_order_id" not in _column_names(conn, "positions")

        _migration_v11(conn)
        conn.commit()

        cols = _column_names(conn, "positions")
        assert "alpaca_exit_order_id" in cols, "V11 must add the column"

        row = conn.execute(
            "SELECT ticker, alpaca_exit_order_id FROM positions"
        ).fetchone()
        assert row == ("SPY", None), "backfilled rows must default to NULL"

        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == 11
    finally:
        conn.close()


@pytest.mark.unit
def test_v11_is_idempotent(db_at_v10: Path) -> None:
    """Running V11 twice must be safe — fresh installs get the column
    from _SCHEMA_SQL on V4, then the V11 migration runs against an
    already-correct schema and must not error."""
    conn = sqlite3.connect(str(db_at_v10))
    try:
        _migration_v11(conn)
        conn.commit()
        # Second run should not raise (and the column should still be
        # there, not duplicated).
        _migration_v11(conn)
        conn.commit()

        rows = conn.execute(
            "PRAGMA table_info(positions)"
        ).fetchall()
        exit_oid_cols = [r for r in rows if r[1] == "alpaca_exit_order_id"]
        assert len(exit_oid_cols) == 1, (
            f"expected exactly one alpaca_exit_order_id column, got "
            f"{len(exit_oid_cols)}"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_column_appears_in_select_star(tmp_path: Path) -> None:
    """VirtualPortfolio.get_open_positions and OrderManager hydration
    both rely on SELECT * — so the column must materialize without
    explicit query changes."""
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, quantity, "
            "entry_price, entry_time, status, hold_type, phase, strategy_id, "
            "alpaca_exit_order_id) "
            "VALUES ('QQQ','NASDAQ','USD',5,200.0,'2026-05-01T10:00:00-04:00',"
            "'CLOSING','swing',1,'overnight_drift','alp-exit-123')"
        )
        conn.commit()

        row = conn.execute("SELECT * FROM positions WHERE ticker='QQQ'").fetchone()
        assert "alpaca_exit_order_id" in row.keys()
        assert row["alpaca_exit_order_id"] == "alp-exit-123"
    finally:
        conn.close()

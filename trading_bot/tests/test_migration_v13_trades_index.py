"""Tests for V13: add ``idx_trades_ticker_entry_time`` composite index.

Pre-V13 ``OrderManager._hydrate_active_orders`` scanned the entire
``trades`` table on every tick to build a ``(ticker, entry_time) →
trade_id`` Python dict. V13 lets the JOIN use the new composite index
so hydration drops from O(trades) to O(active positions).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.migrations import _migration_v13, run_migrations


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA index_list({table})")
    }


@pytest.fixture
def db_at_v12(tmp_path: Path) -> Path:
    """Build a SQLite DB at V12 so we can drive _migration_v13 directly."""
    db = tmp_path / "v12.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM schema_version WHERE version >= 13")
        # Drop the V13 index to mirror a real V12-era schema.
        conn.execute("DROP INDEX IF EXISTS idx_trades_ticker_entry_time")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.unit
def test_fresh_db_at_target_version_has_index(tmp_path: Path) -> None:
    """A clean run_migrations on an empty DB lands at SCHEMA_VERSION
    with the composite index present (via _SCHEMA_SQL)."""
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        indexes = _index_names(conn, "trades")
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert "idx_trades_ticker_entry_time" in indexes
    assert version == SCHEMA_VERSION


@pytest.mark.unit
def test_v13_adds_index_on_existing_db(db_at_v12: Path) -> None:
    """V12 → V13 adds the new composite index; existing rows survive."""
    conn = sqlite3.connect(str(db_at_v12))
    try:
        conn.execute(
            "INSERT INTO trades (ticker, exchange, currency, side, entry_time, "
            "entry_price, quantity, hold_type, phase) "
            "VALUES ('SPY','NYSE','USD','BUY','2026-05-01T10:00:00-04:00',"
            "100.0,10,'intraday',1)"
        )
        conn.commit()
        assert "idx_trades_ticker_entry_time" not in _index_names(conn, "trades")

        _migration_v13(conn)
        conn.commit()

        indexes = _index_names(conn, "trades")
        assert "idx_trades_ticker_entry_time" in indexes, (
            "V13 must add the composite index"
        )

        row = conn.execute(
            "SELECT ticker FROM trades WHERE ticker='SPY'"
        ).fetchone()
        assert row == ("SPY",), "existing rows must survive"

        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == 13
    finally:
        conn.close()


@pytest.mark.unit
def test_v13_is_idempotent(db_at_v12: Path) -> None:
    """Running V13 twice must be safe — fresh installs get the index
    from _SCHEMA_SQL, then the V13 migration runs against an
    already-correct schema and must not error."""
    conn = sqlite3.connect(str(db_at_v12))
    try:
        _migration_v13(conn)
        conn.commit()
        _migration_v13(conn)
        conn.commit()

        indexes = list(conn.execute("PRAGMA index_list(trades)"))
        composite = [
            r for r in indexes if r[1] == "idx_trades_ticker_entry_time"
        ]
        assert len(composite) == 1, (
            f"expected exactly one idx_trades_ticker_entry_time, got "
            f"{len(composite)}"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_index_covers_both_columns(tmp_path: Path) -> None:
    """The composite index must include both ticker and entry_time —
    otherwise the JOIN in _hydrate_active_orders would fall back to
    a per-row scan."""
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        info = list(
            conn.execute("PRAGMA index_info(idx_trades_ticker_entry_time)")
        )
        cols = [row[2] for row in info]
    finally:
        conn.close()
    assert cols == ["ticker", "entry_time"], (
        f"expected (ticker, entry_time), got {cols}"
    )

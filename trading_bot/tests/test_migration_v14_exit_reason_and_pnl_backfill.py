"""Tests for V14: add ``positions.exit_reason`` + backfill ``trades.pnl_usd``.

V14 addresses two correctness bugs uncovered 2026-05-18:

1. ``positions.exit_reason`` — pre-V14 the strategy-supplied exit
   reason lived only on the in-memory ``_ActiveOrder``; the next tick
   rehydrated from DB with ``exit_reason=None`` and fell back to
   ``"strategy_exit"``. Overnight_drift exits then landed as
   ``strategy_exit`` instead of ``overnight_exit``.
2. ``trades.pnl_usd`` backfill — ``_close_position`` wrote
   ``gross_pnl``/``net_pnl`` but forgot ``pnl_usd``. Downstream readers
   (performance metrics, daily-loss circuit breaker) key off
   ``pnl_usd`` and treat NULL as zero. This migration heals already-
   stamped rows; the companion code change writes ``pnl_usd`` going
   forward.

These tests pin:

A. Fresh DB at SCHEMA_VERSION has both columns + clean trades table.
B. Pre-V14 DB upgraded by ``_migration_v14`` adds the column and
   backfills NULL pnl_usd from net_pnl on closed trades.
C. The backfill is bounded: open trades (exit_time IS NULL) are
   untouched, and rows that already had pnl_usd are not overwritten.
D. The migration is idempotent.
E. SELECT * exposes the new column so OrderManager hydration sees it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.migrations import _migration_v14, run_migrations


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


@pytest.fixture
def db_at_v13(tmp_path: Path) -> Path:
    """Build a DB at V13 so we can drive _migration_v14 directly."""
    db = tmp_path / "v13.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM schema_version WHERE version >= 14")
        if "exit_reason" in _column_names(conn, "positions"):
            conn.execute("ALTER TABLE positions DROP COLUMN exit_reason")
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.unit
def test_fresh_db_at_target_version_has_exit_reason_column(tmp_path: Path) -> None:
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
    assert "exit_reason" in cols
    assert version == SCHEMA_VERSION


@pytest.mark.unit
def test_v14_adds_exit_reason_column_on_existing_db(db_at_v13: Path) -> None:
    conn = sqlite3.connect(str(db_at_v13))
    try:
        conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, quantity, "
            "entry_price, entry_time, status, hold_type, phase, strategy_id) "
            "VALUES ('SPY','NYSE','USD',10,100.0,'2026-05-15T10:00:00-04:00',"
            "'POSITION_OPEN','intraday',1,'overnight_drift')"
        )
        conn.commit()
        assert "exit_reason" not in _column_names(conn, "positions")

        _migration_v14(conn)
        conn.commit()

        cols = _column_names(conn, "positions")
        assert "exit_reason" in cols
        row = conn.execute(
            "SELECT ticker, exit_reason FROM positions"
        ).fetchone()
        assert row == ("SPY", None), "backfilled rows must default to NULL"

        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
        assert version == 14
    finally:
        conn.close()


@pytest.mark.unit
def test_v14_backfills_null_pnl_usd_from_net_pnl(db_at_v13: Path) -> None:
    """The 2026-05-18 bug stamped four closed trades with
    ``net_pnl`` populated but ``pnl_usd IS NULL``. V14 heals them."""
    conn = sqlite3.connect(str(db_at_v13))
    try:
        # Mimic the post-bug shape: closed trade, net_pnl set,
        # pnl_usd NULL. (Other columns set so the row is valid.)
        conn.execute(
            "INSERT INTO trades (ticker, exchange, currency, side, "
            "entry_time, entry_price, quantity, exit_time, exit_price, "
            "exit_reason, gross_pnl, net_pnl, pnl_usd, hold_type, phase, "
            "strategy_id) "
            "VALUES ('SPY','NYSE','USD','BUY',"
            "'2026-05-15T15:45:00-04:00',740.22,1,"
            "'2026-05-18T09:35:00-04:00',739.57,"
            "'strategy_exit',-0.65,-0.65,NULL,'swing',1,'overnight_drift')"
        )
        conn.commit()

        _migration_v14(conn)
        conn.commit()

        row = conn.execute(
            "SELECT net_pnl, pnl_usd FROM trades WHERE ticker='SPY'"
        ).fetchone()
        assert row[0] == pytest.approx(-0.65)
        assert row[1] == pytest.approx(-0.65), (
            "V14 must copy net_pnl into NULL pnl_usd"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_v14_backfill_skips_open_trades(db_at_v13: Path) -> None:
    """Open trades (no exit_time) must keep pnl_usd NULL — they have
    no realized P&L yet, and overwriting from net_pnl (which is also
    NULL on open rows) would still leave NULL but conceptually we
    don't touch them at all."""
    conn = sqlite3.connect(str(db_at_v13))
    try:
        conn.execute(
            "INSERT INTO trades (ticker, exchange, currency, side, "
            "entry_time, entry_price, quantity, "
            "gross_pnl, net_pnl, pnl_usd, hold_type, phase, strategy_id) "
            "VALUES ('QQQ','NASDAQ','USD','BUY',"
            "'2026-05-18T15:45:00-04:00',704.65,1,"
            "NULL,NULL,NULL,'swing',1,'overnight_drift')"
        )
        conn.commit()

        _migration_v14(conn)
        conn.commit()

        row = conn.execute(
            "SELECT exit_time, pnl_usd FROM trades WHERE ticker='QQQ'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None, "open trades must keep pnl_usd NULL"
    finally:
        conn.close()


@pytest.mark.unit
def test_v14_backfill_does_not_overwrite_existing_pnl_usd(db_at_v13: Path) -> None:
    """Rows that already have a non-NULL pnl_usd (e.g. backfilled by
    self_improve.alpaca_backfill, or written by post-V14 code) must
    survive untouched even if pnl_usd disagrees with net_pnl."""
    conn = sqlite3.connect(str(db_at_v13))
    try:
        conn.execute(
            "INSERT INTO trades (ticker, exchange, currency, side, "
            "entry_time, entry_price, quantity, exit_time, exit_price, "
            "exit_reason, gross_pnl, net_pnl, pnl_usd, hold_type, phase, "
            "strategy_id) "
            "VALUES ('XLI','NYSE','USD','BUY',"
            "'2026-05-12T11:45:00-04:00',173.01,1,"
            "'2026-05-18T09:45:00-04:00',170.35,"
            "'strategy_exit',-2.66,-2.66,-2.50,'swing',1,'mean_reversion')"
        )
        conn.commit()

        _migration_v14(conn)
        conn.commit()

        row = conn.execute(
            "SELECT net_pnl, pnl_usd FROM trades WHERE ticker='XLI'"
        ).fetchone()
        assert row[0] == pytest.approx(-2.66)
        assert row[1] == pytest.approx(-2.50), (
            "V14 must not overwrite a pre-existing pnl_usd value"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_v14_is_idempotent(db_at_v13: Path) -> None:
    conn = sqlite3.connect(str(db_at_v13))
    try:
        _migration_v14(conn)
        conn.commit()
        _migration_v14(conn)
        conn.commit()

        rows = conn.execute("PRAGMA table_info(positions)").fetchall()
        cols = [r for r in rows if r[1] == "exit_reason"]
        assert len(cols) == 1
    finally:
        conn.close()


@pytest.mark.unit
def test_exit_reason_appears_in_select_star(tmp_path: Path) -> None:
    """OrderManager._hydrate_active_orders does SELECT *. The new
    column must materialize without explicit query changes."""
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, quantity, "
            "entry_price, entry_time, status, hold_type, phase, strategy_id, "
            "alpaca_exit_order_id, exit_reason) "
            "VALUES ('SPY','NYSE','USD',1,740.22,'2026-05-15T15:45:00-04:00',"
            "'CLOSING','swing',1,'overnight_drift','alp-1','overnight_exit')"
        )
        conn.commit()

        row = conn.execute(
            "SELECT * FROM positions WHERE ticker='SPY'"
        ).fetchone()
        assert "exit_reason" in row.keys()
        assert row["exit_reason"] == "overnight_exit"
    finally:
        conn.close()

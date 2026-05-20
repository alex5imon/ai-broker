"""Tests for V15: dedupe phantom backfill rows shadowing live-closed trades.

Bug observed 2026-05-19: every correctly-closed position from
2026-05-15 / 2026-05-18 / 2026-05-19 had **two** rows in ``trades``:
one written by the live ``_close_position`` path with the real
``exit_reason`` (e.g. ``overnight_exit``, ``stop_loss``), and one
inserted later by ``self_improve.alpaca_backfill`` with
``exit_reason='manual'`` and the ``backfill:position:`` notes marker.

The phantom doubled every ``daily_summaries`` total.

V15 deletes the phantoms (and only the phantoms): a backfill-marked
row is removed iff another fully-closed trades row exists for the
same ``(ticker, strategy_id, substr(entry_time, 1, 19))``.

These tests pin:

A. The 5-row dup pattern from 2026-05-19 is collapsed to 5 single rows.
B. Backfill rows that genuinely substitute for a missing live close
   (no non-backfill sibling) survive.
C. Live-closed rows are never deleted, even when paired with a backfill row.
D. The migration is idempotent.
E. Sibling-matching tolerates microsecond drift in entry_time (the
   live writer / backfill writer agree only to second precision).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.migrations import _migration_v15, run_migrations


@pytest.fixture
def db_at_v14(tmp_path: Path) -> Path:
    db = tmp_path / "v14.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("DELETE FROM schema_version WHERE version >= 15")
        conn.commit()
    finally:
        conn.close()
    return db


def _insert_trade(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    strategy_id: str,
    entry_time: str,
    exit_time: str | None,
    exit_price: float | None,
    exit_reason: str | None,
    notes: str | None,
    pnl: float | None = -0.50,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side, entry_time, entry_price,
            quantity, exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, pnl_usd,
            hold_type, phase, strategy_id, notes
        ) VALUES (?, 'NYSE', 'USD', 'BUY', ?, 100.0, 1,
                  ?, ?, ?, ?, ?, ?, 'swing', 1, ?, ?)
        """,
        (
            ticker, entry_time,
            exit_time, exit_price, exit_reason,
            pnl, pnl, pnl,
            strategy_id, notes,
        ),
    )
    return int(cur.lastrowid)


@pytest.mark.unit
def test_fresh_db_at_target_version(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    run_migrations(str(db))
    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == SCHEMA_VERSION


@pytest.mark.unit
def test_v15_deletes_phantom_when_live_sibling_exists(db_at_v14: Path) -> None:
    """The exact pattern observed 2026-05-19: live row with the real
    exit_reason, plus a backfill row with exit_reason='manual'. V15
    must keep the live row and remove the phantom."""
    conn = sqlite3.connect(str(db_at_v14))
    try:
        entry = "2026-05-18T15:45:32.965597-04:00"
        live_id = _insert_trade(
            conn, ticker="SPY", strategy_id="overnight_drift",
            entry_time=entry,
            exit_time="2026-05-19T09:35:34.838039-04:00",
            exit_price=735.05,
            exit_reason="overnight_exit",
            notes=None,
        )
        phantom_id = _insert_trade(
            conn, ticker="SPY", strategy_id="overnight_drift",
            entry_time=entry,
            exit_time="2026-05-19T09:30:37.462232-04:00",
            exit_price=735.05,
            exit_reason="manual",
            notes="backfill:position:42",
        )
        conn.commit()

        _migration_v15(conn)
        conn.commit()

        remaining = {
            r[0] for r in conn.execute("SELECT id FROM trades").fetchall()
        }
        assert remaining == {live_id}, (
            "the phantom must be deleted; the live row must survive"
        )
        kept = conn.execute(
            "SELECT exit_reason FROM trades WHERE id = ?", (live_id,)
        ).fetchone()
        assert kept[0] == "overnight_exit"
        _ = phantom_id  # explicit: we expect the phantom id to be gone
    finally:
        conn.close()


@pytest.mark.unit
def test_v15_preserves_backfill_when_no_live_sibling(db_at_v14: Path) -> None:
    """A position genuinely repaired by backfill (no live-closed
    counterpart) must NOT be deleted. This is the only legitimate
    backfill-INSERT case the migration must protect."""
    conn = sqlite3.connect(str(db_at_v14))
    try:
        only_id = _insert_trade(
            conn, ticker="XLF", strategy_id="mean_reversion",
            entry_time="2026-05-10T10:00:00-04:00",
            exit_time="2026-05-10T11:00:00-04:00",
            exit_price=99.5,
            exit_reason="manual",
            notes="backfill:position:99",
        )
        conn.commit()

        _migration_v15(conn)
        conn.commit()

        remaining = {
            r[0] for r in conn.execute("SELECT id FROM trades").fetchall()
        }
        assert remaining == {only_id}, (
            "a backfill row with no live sibling must survive"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_v15_tolerates_microsecond_drift_in_entry_time(db_at_v14: Path) -> None:
    """The live writer captures sub-second timestamps; the backfill
    writer rounds. V15 must collapse the pair using the 19-char prefix
    match (YYYY-MM-DDTHH:MM:SS)."""
    conn = sqlite3.connect(str(db_at_v14))
    try:
        live_id = _insert_trade(
            conn, ticker="QQQ", strategy_id="overnight_drift",
            entry_time="2026-05-18T15:45:33.085116-04:00",  # microseconds
            exit_time="2026-05-19T09:35:39.092846-04:00",
            exit_price=701.61,
            exit_reason="overnight_exit",
            notes=None,
        )
        _insert_trade(
            conn, ticker="QQQ", strategy_id="overnight_drift",
            entry_time="2026-05-18T15:45:33-04:00",  # second precision only
            exit_time="2026-05-19T09:30:37.606770-04:00",
            exit_price=701.61,
            exit_reason="manual",
            notes="backfill:position:43",
        )
        conn.commit()

        _migration_v15(conn)
        conn.commit()

        remaining = {
            r[0] for r in conn.execute("SELECT id FROM trades").fetchall()
        }
        assert remaining == {live_id}
    finally:
        conn.close()


@pytest.mark.unit
def test_v15_does_not_match_across_strategies(db_at_v14: Path) -> None:
    """Two strategies opening the same ticker at the same second is
    a corner case but must not cross-match. Matching includes
    strategy_id."""
    conn = sqlite3.connect(str(db_at_v14))
    try:
        live_id = _insert_trade(
            conn, ticker="SPY", strategy_id="overnight_drift",
            entry_time="2026-05-18T15:45:32.965597-04:00",
            exit_time="2026-05-19T09:35:34-04:00",
            exit_price=735.05,
            exit_reason="overnight_exit",
            notes=None,
        )
        phantom_id = _insert_trade(
            conn, ticker="SPY", strategy_id="mean_reversion",
            entry_time="2026-05-18T15:45:32.965597-04:00",
            exit_time="2026-05-19T09:30:37-04:00",
            exit_price=735.05,
            exit_reason="manual",
            notes="backfill:position:55",
        )
        conn.commit()

        _migration_v15(conn)
        conn.commit()

        remaining = {
            r[0] for r in conn.execute("SELECT id FROM trades").fetchall()
        }
        assert remaining == {live_id, phantom_id}, (
            "phantom must survive: no matching-strategy live sibling"
        )
    finally:
        conn.close()


@pytest.mark.unit
def test_v15_is_idempotent(db_at_v14: Path) -> None:
    conn = sqlite3.connect(str(db_at_v14))
    try:
        entry = "2026-05-18T15:45:32-04:00"
        _insert_trade(
            conn, ticker="SPY", strategy_id="overnight_drift",
            entry_time=entry,
            exit_time="2026-05-19T09:35-04:00",
            exit_price=735.05,
            exit_reason="overnight_exit",
            notes=None,
        )
        _insert_trade(
            conn, ticker="SPY", strategy_id="overnight_drift",
            entry_time=entry,
            exit_time="2026-05-19T09:30-04:00",
            exit_price=735.05,
            exit_reason="manual",
            notes="backfill:position:42",
        )
        conn.commit()

        _migration_v15(conn)
        conn.commit()
        first_remaining = conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0]

        _migration_v15(conn)
        conn.commit()
        second_remaining = conn.execute(
            "SELECT COUNT(*) FROM trades"
        ).fetchone()[0]

        assert first_remaining == 1
        assert second_remaining == 1, "second run must be a no-op"
    finally:
        conn.close()


@pytest.mark.unit
def test_v15_handles_2026_05_19_pattern_end_to_end(db_at_v14: Path) -> None:
    """Reconstruct the actual 5-row dup pattern observed in the live
    DB on 2026-05-19 and verify V15 collapses it to 5 single rows."""
    conn = sqlite3.connect(str(db_at_v14))
    try:
        # (ticker, strategy, entry_time, real_reason, marker_id)
        pairs = [
            ("GOOGL", "mean_reversion",  "2026-05-15T09:40:44-04:00", "stop_loss",      183),
            ("SPY",   "overnight_drift", "2026-05-15T15:45:32-04:00", "strategy_exit",  184),
            ("QQQ",   "overnight_drift", "2026-05-15T15:45:32-04:00", "strategy_exit",  185),
            ("SPY",   "overnight_drift", "2026-05-18T15:45:32-04:00", "overnight_exit", 186),
            ("QQQ",   "overnight_drift", "2026-05-18T15:45:33-04:00", "overnight_exit", 187),
        ]
        live_ids: list[int] = []
        for ticker, strat, entry, reason, marker in pairs:
            live_ids.append(_insert_trade(
                conn, ticker=ticker, strategy_id=strat,
                entry_time=entry,
                exit_time=entry.replace("15:45", "09:35").replace("09:40", "10:25"),
                exit_price=99.0,
                exit_reason=reason,
                notes=None,
            ))
            _insert_trade(
                conn, ticker=ticker, strategy_id=strat,
                entry_time=entry,
                exit_time=entry.replace("15:45", "09:30").replace("09:40", "10:20"),
                exit_price=99.0,
                exit_reason="manual",
                notes=f"backfill:position:{marker}",
            )
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 10

        _migration_v15(conn)
        conn.commit()

        remaining = {
            r[0] for r in conn.execute("SELECT id FROM trades").fetchall()
        }
        assert remaining == set(live_ids), (
            "all 5 phantoms collapsed; all 5 live rows preserved"
        )
    finally:
        conn.close()

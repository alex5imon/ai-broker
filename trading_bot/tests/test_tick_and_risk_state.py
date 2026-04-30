"""Tests for tick_state / risk_circuit_state persistence (added in Schema V7).

The version assertions reference :data:`trading_bot.constants.SCHEMA_VERSION`
so future migration bumps don't silently regress these tests.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.migrations import run_migrations
from trading_bot.db.repository import (
    load_risk_state,
    load_tick_state,
    save_risk_state,
    save_tick_state,
)


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_creates_new_tables(tmp_path: Path) -> None:
    """run_migrations on a fresh DB creates tick_state + risk_circuit_state."""
    db_path = tmp_path / "fresh.db"
    run_migrations(str(db_path))

    conn = sqlite3.connect(str(db_path))
    try:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "tick_state" in names
        assert "risk_circuit_state" in names

        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """Running migrations twice is a no-op the second time."""
    db_path = tmp_path / "idem.db"
    run_migrations(str(db_path))
    run_migrations(str(db_path))  # must not raise

    conn = sqlite3.connect(str(db_path))
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tick_state
# ---------------------------------------------------------------------------


def test_load_tick_state_missing_returns_none(tmp_db: sqlite3.Connection) -> None:
    assert load_tick_state(tmp_db, "mean_reversion") is None


def test_save_then_load_tick_state(tmp_db: sqlite3.Connection) -> None:
    save_tick_state(
        tmp_db,
        "mean_reversion",
        last_bar_ts="2026-04-24T14:30:00-04:00",
        state={"spread_wait_ticks": 2, "entry_in_progress": True},
    )
    row = load_tick_state(tmp_db, "mean_reversion")
    assert row is not None
    assert row["strategy_id"] == "mean_reversion"
    assert row["last_bar_ts"] == "2026-04-24T14:30:00-04:00"
    assert row["state"] == {"spread_wait_ticks": 2, "entry_in_progress": True}
    assert row["last_run_at"]
    assert row["updated_at"]


def test_save_tick_state_upserts(tmp_db: sqlite3.Connection) -> None:
    """Second save with same strategy_id overwrites, not inserts."""
    save_tick_state(tmp_db, "breakout", last_bar_ts="2026-04-24T14:30:00-04:00")
    save_tick_state(
        tmp_db,
        "breakout",
        last_bar_ts="2026-04-24T14:45:00-04:00",
        state={"pending_order": "abc123"},
    )

    count = tmp_db.execute(
        "SELECT COUNT(*) FROM tick_state WHERE strategy_id = 'breakout'"
    ).fetchone()[0]
    assert count == 1

    row = load_tick_state(tmp_db, "breakout")
    assert row is not None
    assert row["last_bar_ts"] == "2026-04-24T14:45:00-04:00"
    assert row["state"] == {"pending_order": "abc123"}


def test_tick_state_with_no_state_defaults_to_empty(tmp_db: sqlite3.Connection) -> None:
    save_tick_state(tmp_db, "trend", last_bar_ts=None)
    row = load_tick_state(tmp_db, "trend")
    assert row is not None
    assert row["last_bar_ts"] is None
    assert row["state"] == {}


def test_tick_state_corrupt_json_returns_empty(tmp_db: sqlite3.Connection) -> None:
    """A corrupt state_json blob is logged and replaced with {}."""
    tmp_db.execute(
        "INSERT INTO tick_state (strategy_id, last_bar_ts, state_json) "
        "VALUES ('broken', '2026-04-24T14:30:00-04:00', 'not-valid-json')"
    )
    row = load_tick_state(tmp_db, "broken")
    assert row is not None
    assert row["state"] == {}


# ---------------------------------------------------------------------------
# risk_circuit_state
# ---------------------------------------------------------------------------


def test_load_risk_state_missing_returns_none(tmp_db: sqlite3.Connection) -> None:
    assert load_risk_state(tmp_db, "global") is None


def test_save_untripped_risk_state(tmp_db: sqlite3.Connection) -> None:
    save_risk_state(tmp_db, "global", tripped=False, state={"loss_streak": 0})
    row = load_risk_state(tmp_db, "global")
    assert row is not None
    assert row["tripped"] is False
    assert row["tripped_at"] is None
    assert row["reason"] is None
    assert row["state"] == {"loss_streak": 0}


def test_save_tripped_risk_state_records_tripped_at(
    tmp_db: sqlite3.Connection,
) -> None:
    save_risk_state(
        tmp_db,
        "global",
        tripped=True,
        reason="daily drawdown exceeded -2%",
        state={"equity_usd": 900.0},
    )
    row = load_risk_state(tmp_db, "global")
    assert row is not None
    assert row["tripped"] is True
    assert row["tripped_at"] is not None
    assert row["reason"] == "daily drawdown exceeded -2%"
    assert row["state"] == {"equity_usd": 900.0}


def test_tripped_at_preserved_across_updates(tmp_db: sqlite3.Connection) -> None:
    """An already-tripped circuit keeps its original tripped_at when re-saved."""
    save_risk_state(tmp_db, "global", tripped=True, reason="initial")
    first_tripped_at = load_risk_state(tmp_db, "global")["tripped_at"]

    save_risk_state(tmp_db, "global", tripped=True, reason="still tripped")
    second_tripped_at = load_risk_state(tmp_db, "global")["tripped_at"]

    assert first_tripped_at == second_tripped_at


def test_untripping_clears_tripped_at_and_reason(tmp_db: sqlite3.Connection) -> None:
    save_risk_state(tmp_db, "global", tripped=True, reason="hit limit")
    save_risk_state(tmp_db, "global", tripped=False)
    row = load_risk_state(tmp_db, "global")
    assert row is not None
    assert row["tripped"] is False
    assert row["tripped_at"] is None
    assert row["reason"] is None


def test_risk_state_per_strategy_key(tmp_db: sqlite3.Connection) -> None:
    """Different keys are independent rows."""
    save_risk_state(tmp_db, "global", tripped=False)
    save_risk_state(tmp_db, "mean_reversion", tripped=True, reason="3 losses")

    assert load_risk_state(tmp_db, "global")["tripped"] is False
    assert load_risk_state(tmp_db, "mean_reversion")["tripped"] is True
    assert load_risk_state(tmp_db, "mean_reversion")["reason"] == "3 losses"


def test_risk_state_json_roundtrip(tmp_db: sqlite3.Connection) -> None:
    """Nested state dict survives a save/load roundtrip."""
    nested = {
        "counters": {"consecutive_losses": 2, "daily_trades": 4},
        "flags": ["warning_sent"],
    }
    save_risk_state(tmp_db, "mean_reversion", tripped=False, state=nested)
    row = load_risk_state(tmp_db, "mean_reversion")
    assert row is not None
    assert row["state"] == nested

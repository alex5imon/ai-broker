"""Tests for Config.detect_disabled_strategy_orphans.

Surfaces the failure mode that produced the 2026-04-28 orphan incident:
operator flips a sleeve to enabled=false in config.yaml while it still
has live positions, leaving them outside the per-tick management loop
until the drain path catches up.
"""

from __future__ import annotations

import pytest

from trading_bot.config import Config


def _build_config(strategies: dict[str, dict]) -> Config:
    """Construct a Config directly from a raw dict (skips file IO)."""
    raw = {
        "multi_strategy": {
            "enabled": True,
            "strategies": strategies,
        }
    }
    return Config(raw)


def _insert_position(
    conn,
    *,
    ticker: str,
    strategy_id: str,
    status: str = "POSITION_OPEN",
    quantity: float = 1.0,
) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            ticker, exchange, currency, quantity, entry_price,
            entry_time, status, hold_type, phase, strategy_id
        ) VALUES (?, 'NYSE', 'USD', ?, 100.0,
                  '2026-04-27T15:40:00-04:00', ?, 'swing', 1, ?)
        """,
        (ticker, quantity, status, strategy_id),
    )


@pytest.mark.unit
def test_returns_empty_when_all_positions_on_enabled_strategies(tmp_db_path):
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {"enabled": True},
        "breakout": {"enabled": False},
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="mean_reversion")
    conn.commit()
    conn.close()

    assert cfg.detect_disabled_strategy_orphans(tmp_db_path) == []


@pytest.mark.unit
def test_warns_for_open_position_on_disabled_strategy(tmp_db_path):
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {"enabled": True},
        "breakout": {"enabled": False},
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="breakout",
                     status="STOP_AND_TARGET_ACTIVE", quantity=1.0)
    conn.commit()
    conn.close()

    out = cfg.detect_disabled_strategy_orphans(tmp_db_path)
    assert len(out) == 1
    assert "breakout" in out[0]
    assert "SPY" in out[0]


@pytest.mark.unit
def test_skips_closed_and_entry_failed_positions(tmp_db_path):
    """Terminal-state rows aren't live exposure — drain doesn't touch
    them, neither should this guard."""
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {"enabled": True},
        "breakout": {"enabled": False},
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="breakout", status="CLOSED")
    _insert_position(conn, ticker="QQQ", strategy_id="breakout", status="ENTRY_FAILED")
    conn.commit()
    conn.close()

    assert cfg.detect_disabled_strategy_orphans(tmp_db_path) == []


@pytest.mark.unit
def test_skips_unknown_strategy_id(tmp_db_path):
    """'unknown' is the sentinel for unattributed positions (e.g. those
    discovered on Alpaca but never entered by the bot). It's not a
    'disabled strategy' — recovery / drain handle it via different
    paths."""
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {"enabled": True},
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="QQQ", strategy_id="unknown")
    conn.commit()
    conn.close()

    assert cfg.detect_disabled_strategy_orphans(tmp_db_path) == []


@pytest.mark.unit
def test_returns_one_warning_per_position(tmp_db_path):
    """Multiple disabled-strategy positions → multiple warnings, one
    per row, so the operator can act on each individually."""
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {"enabled": True},
        "breakout": {"enabled": False},
        "trend_following": {"enabled": False},
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="breakout", quantity=1.0)
    _insert_position(conn, ticker="XLRE", strategy_id="trend_following", quantity=20.0)
    conn.commit()
    conn.close()

    out = cfg.detect_disabled_strategy_orphans(tmp_db_path)
    assert len(out) == 2
    assert any("breakout" in w and "SPY" in w for w in out)
    assert any("trend_following" in w and "XLRE" in w for w in out)


@pytest.mark.unit
def test_treats_missing_enabled_flag_as_enabled(tmp_db_path):
    """A strategy entry with no 'enabled' key is treated as enabled
    (matches create_strategies() default)."""
    import sqlite3

    cfg = _build_config({
        "mean_reversion": {},  # no 'enabled' key — defaults to True
    })
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="mean_reversion")
    conn.commit()
    conn.close()

    assert cfg.detect_disabled_strategy_orphans(tmp_db_path) == []


@pytest.mark.unit
def test_returns_empty_on_db_failure(tmp_path):
    """If the DB doesn't exist, return empty rather than crashing the
    bot's startup. The check is defensive; failure should be silent
    (with a warning log) rather than fatal."""
    cfg = _build_config({"mean_reversion": {"enabled": True}})
    nonexistent = str(tmp_path / "nonexistent.db")
    # sqlite3.connect creates a file even for a nonexistent path, so
    # we'd actually succeed. Force a real failure with a directory path.
    bad_path = str(tmp_path)  # path is a directory → connect succeeds, query fails
    assert cfg.detect_disabled_strategy_orphans(bad_path) == []


@pytest.mark.unit
def test_warning_message_includes_status_and_qty(tmp_db_path):
    import sqlite3

    cfg = _build_config({"breakout": {"enabled": False}})
    conn = sqlite3.connect(tmp_db_path)
    _insert_position(conn, ticker="SPY", strategy_id="breakout",
                     status="STOP_AND_TARGET_ACTIVE", quantity=1.0)
    conn.commit()
    conn.close()

    out = cfg.detect_disabled_strategy_orphans(tmp_db_path)
    assert len(out) == 1
    msg = out[0]
    assert "STOP_AND_TARGET_ACTIVE" in msg
    assert "1" in msg  # qty

"""Tests for trading_bot.self_improve.flatten_orphans plan-building.

Order submission and Alpaca round-trips are intentionally not exercised
here — those run only behind --execute and require a paper account.
We verify: long/short side derivation, DB child-order discovery,
status filter (don't touch already-CLOSED rows), idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading_bot.self_improve.flatten_orphans import (
    _build_plan,
    _find_db_position,
)


@dataclass
class _FakeAlpacaPosition:
    symbol: str
    qty: str  # alpaca-py returns qty as string


@pytest.mark.unit
def test_long_position_flattens_via_sell():
    plan = _build_plan(
        _FakeAlpacaPosition(symbol="SPY", qty="1.0"),
        db_position_id=42,
        child_order_ids=["stop-1"],
    )
    assert plan.ticker == "SPY"
    assert plan.alpaca_qty == 1.0
    assert plan.flatten_side == "SELL"
    assert plan.flatten_qty == 1.0
    assert plan.db_position_id == 42
    assert plan.child_order_ids_to_cancel == ["stop-1"]


@pytest.mark.unit
def test_short_position_flattens_via_buy():
    plan = _build_plan(
        _FakeAlpacaPosition(symbol="QQQ", qty="-1.0"),
        db_position_id=None,
        child_order_ids=[],
    )
    assert plan.alpaca_qty == -1.0
    assert plan.flatten_side == "BUY"
    assert plan.flatten_qty == 1.0


@pytest.mark.unit
def test_fractional_long_flattens_via_sell_with_full_qty():
    plan = _build_plan(
        _FakeAlpacaPosition(symbol="XLRE", qty="20.5"),
        db_position_id=7,
        child_order_ids=[],
    )
    assert plan.flatten_side == "SELL"
    assert plan.flatten_qty == 20.5


@pytest.mark.unit
def test_find_db_position_returns_none_when_missing(tmp_db):
    pos_id, child_ids = _find_db_position(tmp_db, "GHOST")
    assert pos_id is None
    assert child_ids == []


@pytest.mark.unit
def test_find_db_position_skips_already_closed(tmp_db):
    tmp_db.execute(
        """
        INSERT INTO positions
          (ticker, exchange, currency, quantity, entry_price, entry_time,
           status, hold_type, phase, strategy_id)
        VALUES ('XLRE', 'NYSE', 'USD', 20, 50.0, '2026-04-28T11:50:00-04:00',
                'CLOSED', 'swing', 1, 'trend_following')
        """
    )
    tmp_db.commit()
    pos_id, child_ids = _find_db_position(tmp_db, "XLRE")
    assert pos_id is None
    assert child_ids == []


@pytest.mark.unit
def test_find_db_position_returns_open_row_with_child_orders(tmp_db):
    tmp_db.execute(
        """
        INSERT INTO positions
          (ticker, exchange, currency, quantity, entry_price, entry_time,
           status, hold_type, phase, strategy_id,
           alpaca_stop_order_id, alpaca_target_order_id, alpaca_trail_order_id)
        VALUES ('SPY', 'NYSE', 'USD', 1, 700.0, '2026-04-27T15:40:00-04:00',
                'STOP_AND_TARGET_ACTIVE', 'swing', 1, 'breakout',
                'stop-abc', 'target-def', NULL)
        """
    )
    tmp_db.commit()
    pos_id, child_ids = _find_db_position(tmp_db, "SPY")
    assert pos_id is not None
    assert sorted(child_ids) == ["stop-abc", "target-def"]


@pytest.mark.unit
def test_find_db_position_returns_most_recent_when_history_exists(tmp_db):
    # Older row CLOSED, newer row OPEN — should pick the open one.
    tmp_db.execute(
        """
        INSERT INTO positions
          (ticker, exchange, currency, quantity, entry_price, entry_time,
           status, hold_type, phase, strategy_id)
        VALUES ('QQQ', 'NASDAQ', 'USD', -1, 650.0, '2026-04-20T10:00:00-04:00',
                'CLOSED', 'swing', 1, 'unknown')
        """
    )
    tmp_db.execute(
        """
        INSERT INTO positions
          (ticker, exchange, currency, quantity, entry_price, entry_time,
           status, hold_type, phase, strategy_id)
        VALUES ('QQQ', 'NASDAQ', 'USD', -1, 650.0, '2026-04-27T10:26:29-04:00',
                'POSITION_OPEN', 'swing', 1, 'unknown')
        """
    )
    tmp_db.commit()
    pos_id, _ = _find_db_position(tmp_db, "QQQ")
    assert pos_id is not None
    row = tmp_db.execute(
        "SELECT entry_time FROM positions WHERE id = ?", (pos_id,)
    ).fetchone()
    assert row[0].startswith("2026-04-27")


@pytest.mark.unit
def test_find_db_position_skips_entry_failed(tmp_db):
    tmp_db.execute(
        """
        INSERT INTO positions
          (ticker, exchange, currency, quantity, entry_price, entry_time,
           status, hold_type, phase, strategy_id)
        VALUES ('SPY', 'NYSE', 'USD', 1, 700.0, '2026-04-27T15:40:00-04:00',
                'ENTRY_FAILED', 'swing', 1, 'breakout')
        """
    )
    tmp_db.commit()
    pos_id, child_ids = _find_db_position(tmp_db, "SPY")
    assert pos_id is None
    assert child_ids == []

"""Tests for trading_bot.self_improve.flatten_orphans plan-building.

Order submission and Alpaca round-trips are intentionally not exercised
here — those run only behind --execute and require a paper account.
We verify: long/short side derivation, DB child-order discovery,
status filter (don't touch already-CLOSED rows), idempotency,
and TIF selection from clock state.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from datetime import datetime
from unittest.mock import MagicMock

from trading_bot.self_improve.flatten_orphans import (
    OrphanPlan,
    _build_plan,
    _choose_tif,
    _execute_bulk,
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


# ---------------------------------------------------------------------------
# _choose_tif — clock-aware TimeInForce selection
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_choose_tif_market_open_returns_day():
    from alpaca.trading.enums import TimeInForce
    assert _choose_tif(is_market_open=True) == TimeInForce.DAY


@pytest.mark.unit
def test_choose_tif_market_closed_returns_opg():
    """OPG queues for the opening auction. Note: in practice OPG also
    fails the held_for_orders check pre-market, so the script prefers
    close_all_positions(cancel_orders=True) when planned == live; this
    helper is the per-ticker fallback path."""
    from alpaca.trading.enums import TimeInForce
    assert _choose_tif(is_market_open=False) == TimeInForce.OPG


# ---------------------------------------------------------------------------
# _execute_bulk — broker-atomic close_all_positions path
# ---------------------------------------------------------------------------


def _plan(ticker: str, db_id: int | None = None) -> OrphanPlan:
    return OrphanPlan(
        ticker=ticker,
        alpaca_qty=1.0,
        flatten_side="SELL",
        flatten_qty=1.0,
        db_position_id=db_id,
        child_order_ids_to_cancel=[],
    )


def _bulk_response(symbol: str, status: int, order_id: str = "ord-1"):
    body = MagicMock()
    body.id = order_id
    resp = MagicMock()
    resp.symbol = symbol
    resp.status = status
    resp.body = body
    return resp


@pytest.mark.unit
def test_execute_bulk_marks_db_closed_on_accepted_response(tmp_db):
    tmp_db.execute(
        """INSERT INTO positions
           (id, ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id)
           VALUES (5, 'SPY', 'NYSE', 'USD', 1, 700.0,
                   '2026-04-27T15:40:00-04:00', 'POSITION_OPEN', 'swing', 1, 'breakout')"""
    )
    tmp_db.commit()

    client = MagicMock()
    client.close_all_positions.return_value = [_bulk_response("SPY", 200)]

    failures = _execute_bulk([_plan("SPY", db_id=5)], client, tmp_db)
    assert failures == 0
    client.close_all_positions.assert_called_once_with(cancel_orders=True)
    row = tmp_db.execute("SELECT status FROM positions WHERE id = 5").fetchone()
    assert row[0] == "CLOSED"


@pytest.mark.unit
def test_execute_bulk_counts_failure_on_non_2xx(tmp_db):
    client = MagicMock()
    # Bulk endpoint returned 422 for SPY — broker rejected it.
    client.close_all_positions.return_value = [_bulk_response("SPY", 422)]

    failures = _execute_bulk([_plan("SPY")], client, tmp_db)
    assert failures == 1


@pytest.mark.unit
def test_execute_bulk_counts_failure_when_ticker_missing_from_response(tmp_db):
    client = MagicMock()
    client.close_all_positions.return_value = [_bulk_response("XLRE", 200)]

    failures = _execute_bulk([_plan("SPY"), _plan("XLRE")], client, tmp_db)
    assert failures == 1  # SPY missing from response


@pytest.mark.unit
def test_execute_bulk_handles_endpoint_exception(tmp_db):
    client = MagicMock()
    client.close_all_positions.side_effect = RuntimeError("network down")

    failures = _execute_bulk([_plan("SPY"), _plan("QQQ")], client, tmp_db)
    assert failures == 2  # all plans failed


@pytest.mark.unit
def test_execute_bulk_skips_db_update_when_no_db_id(tmp_db):
    """A planned orphan with db_id=None still counts as success when
    Alpaca accepts the close — there's just no local row to mark."""
    client = MagicMock()
    client.close_all_positions.return_value = [_bulk_response("SPY", 200)]

    failures = _execute_bulk([_plan("SPY", db_id=None)], client, tmp_db)
    assert failures == 0


@pytest.mark.unit
def test_execute_bulk_does_not_mark_db_closed_on_http_202(tmp_db):
    """Regression for review CRITICAL: HTTP 202 = order queued for next
    open. The position is NOT yet flat, so marking the DB row CLOSED
    would corrupt records pre-market. The script should leave the DB
    row OPEN and let the daily reconcile close it after the auction.
    """
    tmp_db.execute(
        """INSERT INTO positions
           (id, ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id)
           VALUES (7, 'SPY', 'NYSE', 'USD', 1, 700.0,
                   '2026-04-27T15:40:00-04:00', 'POSITION_OPEN', 'swing', 1, 'breakout')"""
    )
    tmp_db.commit()

    client = MagicMock()
    # 202 Accepted — Alpaca queued the close for the next open.
    client.close_all_positions.return_value = [_bulk_response("SPY", 202)]

    failures = _execute_bulk([_plan("SPY", db_id=7)], client, tmp_db)
    # 202 is not a failure (the bulk call succeeded), but it is also
    # not a fill — counted as zero failures and DB row stays OPEN.
    assert failures == 0
    row = tmp_db.execute(
        "SELECT status FROM positions WHERE id = 7"
    ).fetchone()
    assert row[0] == "POSITION_OPEN", (
        "HTTP 202 must NOT close the DB row — pre-fix regression"
    )

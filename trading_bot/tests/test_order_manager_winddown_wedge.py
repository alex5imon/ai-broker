"""Watchdog: unwedge a position stuck in CLOSING with no exit order in flight.

Regression for XLI position #147 (2026-06-01 wind-down): an after-hours market
flatten was canceled without persisting its order id, leaving the position
CLOSING + held for 2 days. place_exit / drain_disabled_sleeves refuse to act on
a CLOSING row, and the CLOSING fill-poll needs an exit id — so the row was
unrecoverable. `_check_order_statuses` now rolls such a position back to
STOP_ACTIVE when the broker still holds the shares.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.execution.order_manager import (
    EntryDecision,
    OrderManager,
    _ActiveOrder,
)

pytestmark = pytest.mark.critical


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _entry(ticker: str = "XLI", price: float = 172.76) -> EntryDecision:
    return EntryDecision(
        ticker=ticker, exchange="US", side="BUY", shares=1.0, limit_price=price,
        stop_price=price * 0.98, target_price=price * 1.04,
        hold_type="intraday", sector="Industrials", phase=3,
        sentiment_score=0.0, signals="test", currency="USD",
        strategy_id="opening_range_breakout",
    )


def _seed_closing_no_exit(om: OrderManager, *, qty: float = 0.1143) -> int:
    """Seed a position stuck in CLOSING with no exit order id (the wedge)."""
    trade_id = om._create_position_record(_entry())
    assert trade_id is not None
    active = _ActiveOrder(
        trade_id=trade_id, ticker="XLI", exchange="US", side="BUY",
        status=PositionStatus.CLOSING, entry_shares=qty, filled_shares=qty,
        entry_price=172.76, stop_price=170.0, target_price=180.0,
        db_trade_id=om._pending_db_trade_ids.get(trade_id),
    )
    active.alpaca_exit_order_id = None
    active.alpaca_stop_order_id = None
    om._active_orders[trade_id] = active
    om._update_position_status(trade_id, PositionStatus.CLOSING)
    return trade_id


def _db_status(db_path: str, trade_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (trade_id,)
        ).fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


def _broker_holds(qty: str):
    pos = MagicMock()
    pos.qty = qty
    return MagicMock(return_value=pos)


@pytest.mark.asyncio
async def test_wedged_closing_held_rolls_back_to_stop_active(
    config, tmp_db_path: str, mock_notifier
):
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_closing_no_exit(om)
    # Broker still holds the shares.
    om._gw.client.get_open_position = _broker_holds("0.1143")

    await om._check_order_statuses()

    assert om._active_orders[trade_id].status == PositionStatus.STOP_ACTIVE, (
        "wedged CLOSING+held position must roll back to STOP_ACTIVE"
    )
    assert _db_status(tmp_db_path, trade_id) == PositionStatus.STOP_ACTIVE.value, (
        "rollback must be persisted (stateless tick rehydrates from DB)"
    )


@pytest.mark.asyncio
async def test_wedged_closing_not_held_left_alone(
    config, tmp_db_path: str, mock_notifier
):
    from alpaca.common.exceptions import APIError

    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_closing_no_exit(om)
    # Broker has NO position — it already exited; do not resurrect it.
    om._gw.client.get_open_position = MagicMock(
        side_effect=APIError({"message": "position not found"})
    )

    await om._check_order_statuses()

    assert om._active_orders[trade_id].status == PositionStatus.CLOSING, (
        "not held at broker -> leave CLOSING for the close/recovery path"
    )


@pytest.mark.asyncio
async def test_closing_with_exit_id_not_touched_by_watchdog(
    config, tmp_db_path: str, mock_notifier
):
    """A CLOSING row WITH a live exit order id is a normal in-flight close —
    the watchdog must not roll it back (the fill-poll branch owns it)."""
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_closing_no_exit(om)
    om._active_orders[trade_id].alpaca_exit_order_id = "exit-123"
    # Exit order still working (not filled/canceled).
    pending = MagicMock()
    pending.status.value = "new"
    om._gw.client.get_order_by_id = MagicMock(return_value=pending)
    om._gw.client.get_open_position = _broker_holds("0.1143")

    await om._check_order_statuses()

    assert om._active_orders[trade_id].status == PositionStatus.CLOSING, (
        "an in-flight close (exit id present, order working) must stay CLOSING"
    )

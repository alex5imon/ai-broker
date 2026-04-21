"""Tests for OrderManager state transitions (Alpaca API)."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.execution.order_manager import EntryDecision, OrderManager, _ActiveOrder

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order_manager(
    config,
    db_path: str,
    notifier,
) -> OrderManager:
    gateway = MagicMock()
    gateway.client = MagicMock()
    return OrderManager(gateway, config, notifier, db_path)


def _entry_decision(
    ticker: str = "PLTR",
    exchange: str = "NASDAQ",
    shares: int = 100,
    limit_price: float = 10.0,
    stop_price: float = 9.80,
    target_price: float = 10.30,
) -> EntryDecision:
    return EntryDecision(
        ticker=ticker,
        exchange=exchange,
        side="BUY",
        shares=shares,
        limit_price=limit_price,
        stop_price=stop_price,
        target_price=target_price,
        hold_type="intraday",
        sector="Information Technology",
        phase=1,
        sentiment_score=0.2,
        signals="ema_cross,bb_bounce,volume",
        currency="USD",
    )


def _mock_alpaca_order(
    order_id: str = "abc-123",
    status: str = "new",
    filled_qty: float = 0,
    filled_avg_price: float = 0.0,
    symbol: str = "PLTR",
) -> MagicMock:
    order = MagicMock()
    order.id = order_id
    order.status = MagicMock()
    order.status.value = status
    order.filled_qty = filled_qty
    order.filled_avg_price = filled_avg_price
    order.symbol = symbol
    return order


# ---------------------------------------------------------------------------
# State transition tests
# ---------------------------------------------------------------------------


class TestOrderStateMachine:
    @pytest.mark.asyncio
    async def test_entry_order_placed(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """place_entry submits a limit order via Alpaca client."""
        om = _make_order_manager(config, tmp_db_path, mock_notifier)

        entry_order = _mock_alpaca_order("entry-001", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_order)

        trade_id = await om.place_entry(_entry_decision())
        assert trade_id is not None
        om._gw.client.submit_order.assert_called_once()

        active = om._active_orders.get(trade_id)
        assert active is not None
        assert active.status == PositionStatus.ENTRY_PENDING
        assert active.alpaca_entry_order_id == "entry-001"

    @pytest.mark.asyncio
    async def test_entry_fill_transitions_to_open(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """When polling detects entry filled, status transitions and stop/target placed."""
        om = _make_order_manager(config, tmp_db_path, mock_notifier)

        # Place entry
        entry_order = _mock_alpaca_order("entry-001", "new")
        stop_order = _mock_alpaca_order("stop-001", "new")
        target_order = _mock_alpaca_order("target-001", "new")
        om._gw.client.submit_order = MagicMock(
            side_effect=[entry_order, stop_order, target_order]
        )

        trade_id = await om.place_entry(_entry_decision())
        active = om._active_orders[trade_id]

        # Simulate polling: entry order now filled
        filled_order = _mock_alpaca_order(
            "entry-001", "filled", filled_qty=100, filled_avg_price=10.0
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=filled_order)

        await om._check_order_statuses()

        # Should have placed stop and target orders
        assert om._gw.client.submit_order.call_count >= 2

    @pytest.mark.asyncio
    async def test_entry_rejection_closes_position(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """Entry order rejected → position closed, no retry."""
        om = _make_order_manager(config, tmp_db_path, mock_notifier)

        entry_order = _mock_alpaca_order("entry-001", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_order)

        trade_id = await om.place_entry(_entry_decision())
        initial_call_count = om._gw.client.submit_order.call_count

        # Simulate polling: entry order rejected
        rejected_order = _mock_alpaca_order("entry-001", "rejected")
        om._gw.client.get_order_by_id = MagicMock(return_value=rejected_order)

        await om._check_order_statuses()

        # No additional submit_order calls (no retry)
        assert om._gw.client.submit_order.call_count == initial_call_count
        # Position removed from active orders
        assert trade_id not in om._active_orders

    @pytest.mark.asyncio
    async def test_cancel_all_for_ticker(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """cancel_all_for_ticker cancels matching orders via Alpaca."""
        om = _make_order_manager(config, tmp_db_path, mock_notifier)

        # Mock get_orders to return orders for filtering
        order1 = _mock_alpaca_order("ord-1", "new", symbol="PLTR")
        order2 = _mock_alpaca_order("ord-2", "new", symbol="PLTR")
        order3 = _mock_alpaca_order("ord-3", "new", symbol="F")

        om._gw.client.get_orders = MagicMock(return_value=[order1, order2])
        om._gw.client.cancel_order_by_id = MagicMock()

        await om.cancel_all_for_ticker("PLTR")

        # Should have cancelled the PLTR orders
        assert om._gw.client.cancel_order_by_id.call_count == 2

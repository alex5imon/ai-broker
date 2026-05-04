"""Extended OrderManager tests: entry timeouts, partial fills, exit detection,
trail activation, hydration, and cancel/flatten paths."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.execution.order_manager import EntryDecision, OrderManager, _ActiveOrder

pytestmark = pytest.mark.critical

ET = TZ_EASTERN


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _entry(ticker: str = "SPY", shares: float = 100, price: float = 10.0) -> EntryDecision:
    return EntryDecision(
        ticker=ticker,
        exchange="US",
        side="BUY",
        shares=int(shares),
        limit_price=price,
        stop_price=price * 0.98,
        target_price=price * 1.04,
        hold_type="intraday",
        sector="Information Technology",
        phase=1,
        sentiment_score=0.2,
        signals="test",
        currency="USD",
        strategy_id="mean_reversion",
        trail_pct=0.02,
        trail_activation_price=price * 1.02,
    )


def _alpaca_order(
    order_id: str = "order-1",
    status: str = "new",
    filled_qty: float = 0.0,
    filled_avg_price: float = 0.0,
    submitted_at: datetime | None = None,
    legs: list[Any] | None = None,
):
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.submitted_at = submitted_at
    o.legs = legs
    return o


# ---------------------------------------------------------------------------
# Hydration
# ---------------------------------------------------------------------------


class TestHydration:
    def test_hydrate_loads_positions_from_db(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Pre-populate a position row via the same helper the production code uses.
        om._create_position_record(_entry("AAPL"))

        om._hydrate_active_orders()
        assert len(om._active_orders) == 1
        active = next(iter(om._active_orders.values()))
        assert active.ticker == "AAPL"
        assert active.status == PositionStatus.ENTRY_PENDING

    def test_hydrate_skips_closed(self, config, tmp_db_path: str, mock_notifier):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry("AAPL"))
        om._update_position_status(trade_id, PositionStatus.CLOSED)
        om._hydrate_active_orders()
        assert len(om._active_orders) == 0


# ---------------------------------------------------------------------------
# Entry fill → standalone stop attach
# ---------------------------------------------------------------------------


class TestEntryFill:
    @pytest.mark.asyncio
    async def test_fill_attaches_standalone_stop(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """On fill, a single StopOrderRequest is submitted (no take-profit).
        Take-profit is managed by the tick-loop's check_exits polling against
        target_price, not by the broker."""
        from alpaca.trading.requests import StopOrderRequest

        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _submit(order_data):
            captured.append(order_data)
            order_id = f"order-{len(captured)}"
            return _alpaca_order(order_id, "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)
        trade_id = await om.place_entry(_entry())

        filled_entry = _alpaca_order(
            "order-1", "filled", filled_qty=100, filled_avg_price=10.0,
        )

        def lookup(oid: str):
            if oid == "order-1":
                return filled_entry
            return _alpaca_order(oid, "new")
        om._gw.client.get_order_by_id = MagicMock(side_effect=lookup)

        await om._check_order_statuses()

        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_AND_TARGET_ACTIVE
        # Second submit_order call was the standalone stop
        assert len(captured) == 2
        stop_req = captured[1]
        assert isinstance(stop_req, StopOrderRequest)
        assert active.alpaca_stop_order_id == "order-2"
        # No take-profit submitted to the broker
        assert active.alpaca_target_order_id is None
        mock_notifier.trade_entry.assert_awaited()

    @pytest.mark.asyncio
    async def test_fractional_fill_uses_day_tif_for_stop(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Regression: 2026-05-04 incident.

        Alpaca rejects stop orders on fractional positions with TIF != DAY
        (error 42210000). Without this guard, every fractional entry hit
        the failure path → emergency_flatten → orphan drain on next tick,
        wiping today's strategy attribution.
        """
        from alpaca.trading.enums import TimeInForce
        from alpaca.trading.requests import StopOrderRequest
        from trading_bot.execution.order_manager import EntryDecision

        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _submit(order_data):
            captured.append(order_data)
            return _alpaca_order(f"order-{len(captured)}", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)

        # Fractional qty mirrors today's XLY entry: 4.2067 shares.
        decision = EntryDecision(
            ticker="XLY", exchange="US", side="BUY",
            shares=4.2067, limit_price=117.67,
            stop_price=115.90, target_price=120.0,
            hold_type="intraday", sector="Consumer Discretionary",
            phase=3, sentiment_score=0.0, signals="test",
            currency="USD", strategy_id="mean_reversion",
            trail_pct=None, trail_activation_price=None,
        )
        trade_id = await om.place_entry(decision)

        filled = _alpaca_order(
            "order-1", "filled", filled_qty=4.2067, filled_avg_price=117.67,
        )
        om._gw.client.get_order_by_id = MagicMock(
            side_effect=lambda oid: filled if oid == "order-1" else _alpaca_order(oid, "new"),
        )

        await om._check_order_statuses()

        # Stop order was submitted with DAY TIF, not GTC.
        stop_req = captured[1]
        assert isinstance(stop_req, StopOrderRequest)
        assert stop_req.time_in_force == TimeInForce.DAY, (
            "fractional stop must use DAY TIF (Alpaca rejects GTC for "
            "fractional with error 42210000)"
        )
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_AND_TARGET_ACTIVE

    @pytest.mark.asyncio
    async def test_stop_attach_failure_triggers_emergency_flatten(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """If the standalone stop submit fails, the position is unprotected.
        Recovery: emergency_flatten + notify + collapse to terminal state so
        the next stateless tick does not re-evaluate a ghost POSITION_OPEN
        row with no broker-side protection."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        call_count = {"n": 0}

        def _submit(order_data):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _alpaca_order("entry-1", "new")
            if call_count["n"] == 2:
                # Stop submit fails
                raise RuntimeError("stop submit rejected")
            # Third call: emergency flatten market order
            return _alpaca_order("flatten-1", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)
        trade_id = await om.place_entry(_entry())

        filled = _alpaca_order(
            "entry-1", "filled", filled_qty=100, filled_avg_price=10.0,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=filled)

        await om._check_order_statuses()

        # entry + (failed) stop + emergency flatten = 3 submit_order calls
        assert call_count["n"] == 3
        # Surfaced to the operator as a high-priority notification
        mock_notifier.send.assert_awaited()
        # State machine collapsed cleanly — next tick won't re-evaluate.
        assert trade_id not in om._active_orders
        # Misleading "trade entered" notification is suppressed for a
        # position that was force-flattened immediately after fill.
        mock_notifier.trade_entry.assert_not_awaited()
        # DB row at terminal status, hydration won't reload it.
        conn = sqlite3.connect(tmp_db_path)
        try:
            status = conn.execute(
                "SELECT status FROM positions WHERE id = ?", (trade_id,),
            ).fetchone()[0]
        finally:
            conn.close()
        assert status == PositionStatus.ENTRY_FAILED.value


# ---------------------------------------------------------------------------
# Entry timeout / partial fill
# ---------------------------------------------------------------------------


class TestEntryTimeout:
    @pytest.mark.asyncio
    async def test_partial_fill_above_min_pct_accepted(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # submit_order is called twice: entry, then standalone stop on partial fill.
        om._gw.client.submit_order = MagicMock(
            side_effect=[
                _alpaca_order("entry-1", "new"),
                _alpaca_order("stop-1", "new"),
            ]
        )
        trade_id = await om.place_entry(_entry(shares=100))

        # Partial fill of 80 shares (> 50% default), submitted long ago
        old_submitted = datetime.now(tz=ET) - timedelta(seconds=600)
        partial = _alpaca_order(
            "entry-1", "partially_filled",
            filled_qty=80.0, filled_avg_price=10.0,
            submitted_at=old_submitted,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=partial)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()

        # Cancelled the timed-out entry
        om._gw.client.cancel_order_by_id.assert_called()
        # Position transitioned to STOP_AND_TARGET_ACTIVE with standalone stop
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_AND_TARGET_ACTIVE
        assert active.alpaca_stop_order_id == "stop-1"
        assert active.alpaca_target_order_id is None

    @pytest.mark.asyncio
    async def test_partial_fill_below_min_pct_flattens(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        entry_alpaca = _alpaca_order("entry-1", "new")
        flatten_order = _alpaca_order("flatten-1", "new")
        om._gw.client.submit_order = MagicMock(
            side_effect=[entry_alpaca, flatten_order]
        )
        trade_id = await om.place_entry(_entry(shares=100))

        # Partial fill of 10 shares (< 50% default)
        old_submitted = datetime.now(tz=ET) - timedelta(seconds=600)
        partial = _alpaca_order(
            "entry-1", "partially_filled",
            filled_qty=10.0, filled_avg_price=10.0,
            submitted_at=old_submitted,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=partial)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()

        # Both cancel + emergency flatten happened
        om._gw.client.cancel_order_by_id.assert_called()
        # Emergency flatten = 2nd submit_order call
        assert om._gw.client.submit_order.call_count == 2
        # Position removed from active orders
        assert trade_id not in om._active_orders

    @pytest.mark.asyncio
    async def test_recent_pending_order_not_timed_out(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        entry_alpaca = _alpaca_order("entry-1", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        trade_id = await om.place_entry(_entry())

        recent = _alpaca_order(
            "entry-1", "new", submitted_at=datetime.now(tz=ET),
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=recent)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()

        om._gw.client.cancel_order_by_id.assert_not_called()
        assert trade_id in om._active_orders

    @pytest.mark.asyncio
    async def test_naive_submitted_at_assumes_eastern(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """When Alpaca returns a naive datetime, we assume ET and time-out correctly."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        entry_alpaca = _alpaca_order("entry-1", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        await om.place_entry(_entry(shares=100))

        # Add stop submit response for the partial-fill transition.
        om._gw.client.submit_order = MagicMock(
            side_effect=[_alpaca_order("stop-1", "new")]
        )

        # Naive datetime from "long ago"
        naive_old = datetime.now(tz=ET).replace(tzinfo=None) - timedelta(seconds=600)
        partial = _alpaca_order(
            "entry-1", "partially_filled",
            filled_qty=80.0, filled_avg_price=10.0,
            submitted_at=naive_old,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=partial)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()
        om._gw.client.cancel_order_by_id.assert_called()


# ---------------------------------------------------------------------------
# Exit fill detection
# ---------------------------------------------------------------------------


class TestExitFill:
    @pytest.mark.asyncio
    async def test_stop_loss_fill_closes_position(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Manually wire an active position post-entry-fill
        trade_id = om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=trade_id,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_AND_TARGET_ACTIVE)

        # Stop fills @ 9.80, target still open (gets cancelled)
        def get_order_by_id(oid: str):
            if oid == "stop-1":
                return _alpaca_order(oid, "filled", filled_qty=100, filled_avg_price=9.80)
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=get_order_by_id)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()

        assert active.status == PositionStatus.CLOSED
        # Cancelled the still-open target leg
        om._gw.client.cancel_order_by_id.assert_called_with("target-1")
        mock_notifier.position_closed.assert_awaited()

    @pytest.mark.asyncio
    async def test_target_fill_closes_position(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=trade_id,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_AND_TARGET_ACTIVE)

        def get_order_by_id(oid: str):
            if oid == "target-1":
                return _alpaca_order(oid, "filled", filled_qty=100, filled_avg_price=10.40)
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=get_order_by_id)
        om._gw.client.cancel_order_by_id = MagicMock()

        await om._check_order_statuses()
        assert active.status == PositionStatus.CLOSED


# ---------------------------------------------------------------------------
# Trailing stop activation
# ---------------------------------------------------------------------------


class TestTrailActivation:
    @pytest.mark.asyncio
    async def test_activates_when_price_above_threshold(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=trade_id, ticker="SPY", exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
            trail_pct=0.02, trail_activation_price=10.20,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_AND_TARGET_ACTIVE)

        # Trailing-stop placement returns an order id
        om._gw.client.submit_order = MagicMock(return_value=_alpaca_order("trail-1", "new"))
        om._gw.client.cancel_order_by_id = MagicMock()

        n = await om.check_trail_activations(get_latest_price=lambda t: 10.50)
        assert n == 1
        assert active.trail_activated is True
        assert active.status == PositionStatus.TRAILING_ACTIVE

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_activate(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=trade_id, ticker="SPY", exchange="US",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0,
            trail_pct=0.02, trail_activation_price=10.20,
        )
        om._active_orders[trade_id] = active
        n = await om.check_trail_activations(get_latest_price=lambda t: 10.10)
        assert n == 0
        assert active.trail_activated is False

    @pytest.mark.asyncio
    async def test_get_price_error_skips(self, config, tmp_db_path: str, mock_notifier):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=trade_id, ticker="SPY", exchange="US",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=100, filled_shares=100,
            trail_pct=0.02, trail_activation_price=10.20,
        )
        om._active_orders[trade_id] = active

        def boom(t: str):
            raise RuntimeError("md down")
        n = await om.check_trail_activations(get_latest_price=boom)
        assert n == 0


# ---------------------------------------------------------------------------
# Cancel + flatten paths
# ---------------------------------------------------------------------------


class TestCancelFlatten:
    @pytest.mark.asyncio
    async def test_cancel_order_swallows_alpaca_errors(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.cancel_order_by_id = MagicMock(side_effect=RuntimeError("already done"))
        # Should not raise.
        await om.cancel_order("order-x")

    @pytest.mark.asyncio
    async def test_emergency_flatten_notifies_on_failure(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("api down"))
        await om.emergency_flatten("SPY", 100, "US")
        mock_notifier.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_flatten_all_calls_close_all_positions(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.close_all_positions = MagicMock()
        await om.flatten_all()
        om._gw.client.close_all_positions.assert_called_once_with(cancel_orders=True)

    @pytest.mark.asyncio
    async def test_flatten_all_swallows_errors(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.close_all_positions = MagicMock(side_effect=RuntimeError("nope"))
        # Must not raise.
        await om.flatten_all()


# ---------------------------------------------------------------------------
# Strategy-driven exits (place_exit)
# ---------------------------------------------------------------------------


class TestPlaceExit:
    @pytest.mark.asyncio
    async def test_place_exit_cancels_bracket_legs_and_submits_market_sell(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Track an in-flight position with both bracket legs known.
        active = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        om._active_orders[1] = active

        cancel_calls: list[str] = []
        om._gw.client.cancel_order_by_id = MagicMock(
            side_effect=lambda oid: cancel_calls.append(oid),
        )
        submit_orders: list[Any] = []

        def _submit(*args, **kwargs):
            submit_orders.append(kwargs.get("order_data"))
            o = MagicMock()
            o.id = "exit-1"
            return o

        om._gw.client.submit_order = MagicMock(side_effect=_submit)

        order_id = await om.place_exit(
            ticker="SPY", qty=10, reason="rsi_normalized",
        )
        assert order_id == "exit-1"
        # Both bracket legs cancelled before the new market order
        assert "stop-1" in cancel_calls
        assert "target-1" in cancel_calls
        # Market sell submitted
        assert len(submit_orders) == 1
        # New order id mapped to the original trade id
        assert om._alpaca_to_trade.get("exit-1") == 1

    @pytest.mark.asyncio
    async def test_place_exit_returns_none_on_broker_failure(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("rejected"))
        # No active order tracked → falls through to symbol-wide cancel.
        om._gw.client.get_orders = MagicMock(return_value=[])

        order_id = await om.place_exit(
            ticker="SPY", qty=5, reason="trailing_stop", is_emergency=True,
        )
        assert order_id is None
        # Emergency exits should fire a notification on failure
        mock_notifier.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_place_exit_rejects_non_positive_qty(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock()
        order_id = await om.place_exit(
            ticker="SPY", qty=0, reason="anything",
        )
        assert order_id is None
        om._gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_exit_refuses_when_only_match_is_entry_failed(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Regression for review CRITICAL-1: a position whose only
        in-memory entry is in ENTRY_FAILED status must NOT be matched —
        submitting a market SELL would short a never-filled position.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._active_orders[1] = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.ENTRY_FAILED,
            entry_shares=10.0,
            filled_shares=0.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        om._gw.client.submit_order = MagicMock()

        order_id = await om.place_exit(
            ticker="SPY", qty=10, reason="orphan_drain",
        )

        assert order_id is None
        om._gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_exit_refuses_when_only_match_is_closing(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Regression: a position already CLOSING (exit in flight) must
        not be matched — duplicate SELL would over-flatten."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._active_orders[1] = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.CLOSING,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        om._gw.client.submit_order = MagicMock()

        order_id = await om.place_exit(
            ticker="SPY", qty=10, reason="exit_signal",
        )

        assert order_id is None
        om._gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_exit_transitions_position_to_closing(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Regression for review CRITICAL-3 (mirror of place_limit_exit):
        a successful market SELL must transition the position to CLOSING
        synchronously so the next stateless tick can't re-fire the same
        exit signal and double-submit.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Seed a real DB row so the status update has something to find.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, sector, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SPY", "NASDAQ", "USD", "Information Technology",
                10, 100.0, datetime.now(tz=ET).isoformat(),
                "POSITION_OPEN", "intraday", 1, "mean_reversion",
            ),
        )
        trade_id = conn.execute(
            "SELECT id FROM positions WHERE ticker='SPY'"
        ).fetchone()[0]
        conn.commit()
        conn.close()

        om._active_orders[trade_id] = _ActiveOrder(
            trade_id=trade_id,
            ticker="SPY",
            exchange="NASDAQ",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        submitted = MagicMock()
        submitted.id = "exit-mkt-1"
        om._gw.client.submit_order = MagicMock(return_value=submitted)

        order_id = await om.place_exit(
            ticker="SPY", qty=10, reason="rsi_normalized",
        )
        assert order_id == "exit-mkt-1"
        # In-memory + DB must transition together.
        assert (
            om._active_orders[trade_id].status == PositionStatus.CLOSING
        )
        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (trade_id,),
        ).fetchone()
        conn.close()
        assert row[0] == PositionStatus.CLOSING.value


# ---------------------------------------------------------------------------
# Place-entry failure path
# ---------------------------------------------------------------------------


class TestPlaceEntryFailure:
    @pytest.mark.asyncio
    async def test_alpaca_submit_failure_returns_none(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """place_entry returns None on submit failure. The position row's
        terminal status (ENTRY_FAILED) is asserted in
        test_phase3_regressions.TestB2_FailedEntryGetsEntryFailedStatus.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("rejected"))
        trade_id = await om.place_entry(_entry())
        assert trade_id is None  # Returned None on failure

    @pytest.mark.asyncio
    async def test_submit_failure_logs_to_order_rejections(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """2026-04-29 incident: rejections went straight to status=CLOSED with no
        forensic trail. Now every rejection must persist to order_rejections."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("insufficient buying power"))

        await om.place_entry(_entry(ticker="XLF", shares=10, price=51.0))

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT ticker, order_type, intended_price, intended_qty, reason "
                "FROM order_rejections ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "no order_rejections row written"
        ticker, order_type, intended_price, intended_qty, reason = row
        assert ticker == "XLF"
        assert order_type == "ENTRY"
        assert abs(float(intended_price) - 51.0) < 1e-6
        assert int(intended_qty) == 10
        assert "insufficient buying power" in reason
        assert reason.startswith("alpaca_submit_error")

    @pytest.mark.asyncio
    async def test_entry_failure_marks_trades_row_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """2026-05-01 incident: every fractional+bracket entry was rejected by
        Alpaca, but the trades row inserted by _create_position_record was left
        with no exit_time/exit_reason, polluting daily_summaries (60 phantom
        'closed' rows in 2026-04-30 with NULL net_pnl). The exception handler
        must close the trades row at exit_reason='entry_failed', net_pnl=0."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("boom"))

        await om.place_entry(_entry(ticker="XLF", shares=10, price=51.0))

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT exit_time, exit_price, exit_reason, gross_pnl, net_pnl, pnl_usd "
                "FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()

        assert row is not None, "trades row missing"
        exit_time, exit_price, exit_reason, gross_pnl, net_pnl, pnl_usd = row
        assert exit_time is not None, "trades.exit_time still NULL — phantom row"
        assert abs(float(exit_price) - 51.0) < 1e-6
        assert exit_reason == "entry_failed"
        assert float(gross_pnl) == 0.0
        assert float(net_pnl) == 0.0
        assert float(pnl_usd) == 0.0


class TestFractionalEntry:
    """ai-broker#39: Plain-limit + standalone-stop entry path supports fractional
    shares end-to-end. The previous whole-share floor (PR #38) was a stopgap that
    dropped ~50% of mean_reversion signals on a $1k account; it has been removed
    in favor of submitting a non-bracketed limit order followed by a standalone
    stop on fill."""

    @pytest.mark.asyncio
    async def test_fractional_shares_passed_through(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Fractional shares survive submit untouched — no implicit floor."""
        from alpaca.trading.requests import LimitOrderRequest

        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _capture(order_data):
            captured.append(order_data)
            return _alpaca_order("entry-1", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_capture)

        decision = _entry(ticker="XLF", shares=10, price=51.0)
        decision.shares = 2.7
        trade_id = await om.place_entry(decision)

        assert trade_id is not None
        assert len(captured) == 1
        req = captured[0]
        assert isinstance(req, LimitOrderRequest)
        # Plain limit — no order_class, no children
        assert getattr(req, "order_class", None) is None
        assert getattr(req, "stop_loss", None) is None
        assert getattr(req, "take_profit", None) is None
        # Quantity flowed through as-is (no floor)
        assert float(req.qty) == 2.7

    @pytest.mark.asyncio
    async def test_sub_one_share_signal_not_dropped(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Sub-1-share fractional decision still reaches the broker.
        Pre-fix the whole-share floor turned this into a no-op."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _capture(order_data):
            captured.append(order_data)
            return _alpaca_order("entry-1", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_capture)

        decision = _entry(ticker="SPY", shares=1, price=700.0)
        decision.shares = 0.43
        trade_id = await om.place_entry(decision)

        assert trade_id is not None
        assert len(captured) == 1
        assert float(captured[0].qty) == 0.43

    @pytest.mark.asyncio
    async def test_non_positive_shares_skipped_without_db_insert(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """shares <= 0 is a degenerate sizing result; no order or DB row."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock()

        decision = _entry(ticker="SPY", shares=1, price=700.0)
        decision.shares = 0.0
        trade_id = await om.place_entry(decision)

        assert trade_id is None
        om._gw.client.submit_order.assert_not_called()
        # No DB pollution — neither a position nor a trades row.
        conn = sqlite3.connect(tmp_db_path)
        try:
            n_pos = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
            n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        finally:
            conn.close()
        assert n_pos == 0
        assert n_trades == 0

    @pytest.mark.asyncio
    async def test_fractional_fill_attaches_standalone_stop(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """End-to-end: fractional entry → fill → standalone stop with the
        same fractional qty. Verifies the issue#39 acceptance path."""
        from alpaca.trading.requests import StopOrderRequest

        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _submit(order_data):
            captured.append(order_data)
            order_id = f"order-{len(captured)}"
            return _alpaca_order(order_id, "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)

        decision = _entry(ticker="SPY", shares=1, price=700.0)
        decision.shares = 0.43
        trade_id = await om.place_entry(decision)

        # Fill the entry at the (fractional) decision quantity
        filled = _alpaca_order(
            "order-1", "filled", filled_qty=0.43, filled_avg_price=700.0,
        )

        def lookup(oid: str):
            if oid == "order-1":
                return filled
            return _alpaca_order(oid, "new")
        om._gw.client.get_order_by_id = MagicMock(side_effect=lookup)

        await om._check_order_statuses()

        # Two submit calls: the limit entry, then the standalone stop.
        assert len(captured) == 2
        stop_req = captured[1]
        assert isinstance(stop_req, StopOrderRequest)
        assert float(stop_req.qty) == 0.43
        active = om._active_orders[trade_id]
        assert active.alpaca_stop_order_id == "order-2"
        assert active.alpaca_target_order_id is None

    @pytest.mark.asyncio
    async def test_alpaca_42210000_simulated_no_rejection_under_new_path(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Integration check: a stubbed Alpaca that rejects fractional+bracket
        with code 42210000 but accepts plain LimitOrderRequest must produce
        zero rejections under the new entry path. This is the regression gate
        the issue's acceptance criteria call out."""
        from alpaca.trading.requests import LimitOrderRequest, StopOrderRequest

        om = _make_om(config, tmp_db_path, mock_notifier)
        captured: list = []

        def _stub_alpaca(order_data):
            captured.append(order_data)
            # Simulate Alpaca's real rejection for any fractional+bracket combo.
            is_bracket = bool(getattr(order_data, "order_class", None))
            qty = float(getattr(order_data, "qty", 0) or 0)
            if is_bracket and qty != int(qty):
                raise RuntimeError(
                    "{'code':42210000,'message':'fractional orders must be simple orders'}"
                )
            return _alpaca_order(f"order-{len(captured)}", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_stub_alpaca)

        decision = _entry(ticker="SPY", shares=1, price=700.0)
        decision.shares = 0.43
        trade_id = await om.place_entry(decision)

        # Fractional entry survived without 42210000 rejection.
        assert trade_id is not None

        # On fill, the standalone stop is also accepted.
        filled = _alpaca_order(
            "order-1", "filled", filled_qty=0.43, filled_avg_price=700.0,
        )
        om._gw.client.get_order_by_id = MagicMock(
            side_effect=lambda oid: filled if oid == "order-1"
            else _alpaca_order(oid, "new")
        )

        await om._check_order_statuses()

        # Both submitted requests are simple (non-bracket); neither tripped 42210000.
        assert len(captured) == 2
        assert isinstance(captured[0], LimitOrderRequest)
        assert isinstance(captured[1], StopOrderRequest)
        for req in captured:
            assert getattr(req, "order_class", None) is None


# ---------------------------------------------------------------------------
# get_active_orders helpers
# ---------------------------------------------------------------------------


def test_active_count_and_getters(config, tmp_db_path: str, mock_notifier):
    om = _make_om(config, tmp_db_path, mock_notifier)
    assert om.active_count == 0
    assert om.get_active_orders() == {}
    assert om.get_active_order(99) is None


# ---------------------------------------------------------------------------
# place_limit_exit — prevents double-exit by transitioning state before tick
# ---------------------------------------------------------------------------


class TestPlaceLimitExit:
    """The exit-path regression test for the double-submit bug.

    Pre-fix, ``check_exits`` and ``wind_down`` called
    ``self._gateway.client.submit_order`` directly: the order id was
    discarded and the position row stayed at ``POSITION_OPEN``, so the
    next stateless tick re-evaluated the same exit condition and
    re-submitted.  Routing through ``OrderManager.place_limit_exit``
    fixes both: the order_id is mapped back to the trade and the
    position transitions to ``CLOSING`` synchronously.
    """

    @pytest.mark.asyncio
    async def test_transitions_position_to_closing_before_returning(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)

        # Seed an open position so the OrderManager has something to match.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, sector, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SPY", "NASDAQ", "USD", "Information Technology",
                10, 100.0, datetime.now(tz=ET).isoformat(),
                "POSITION_OPEN", "intraday", 1, "mean_reversion",
            ),
        )
        trade_id = conn.execute(
            "SELECT id FROM positions WHERE ticker = 'SPY'"
        ).fetchone()[0]
        conn.commit()
        conn.close()

        om._active_orders[trade_id] = _ActiveOrder(
            trade_id=trade_id,
            ticker="SPY",
            exchange="NASDAQ",
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.POSITION_OPEN,
            entry_shares=10,
            filled_shares=10,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
        )

        # Stub Alpaca — limit submit returns an order id.
        submitted = _alpaca_order("limit-exit-1", status="new")
        om._gw.client.submit_order = MagicMock(return_value=submitted)

        order_id = await om.place_limit_exit(
            ticker="SPY", qty=10, limit_price=100.5, reason="take_profit",
        )

        assert order_id == "limit-exit-1"

        # CRITICAL: position must be CLOSING in the DB before the next tick.
        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (trade_id,),
        ).fetchone()
        conn.close()
        assert row[0] == PositionStatus.CLOSING.value, (
            f"position must transition to CLOSING before returning, got {row[0]}"
        )

        # And the order_id must be pinned back to the trade so a subsequent
        # fill notification routes to the right position.
        assert om._alpaca_to_trade["limit-exit-1"] == trade_id

    @pytest.mark.asyncio
    async def test_returns_none_on_alpaca_error(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("boom"))

        order_id = await om.place_limit_exit(
            ticker="SPY", qty=10, limit_price=100.0, reason="time_stop",
        )

        # Caller is expected to fall back to emergency_flatten when None.
        assert order_id is None

    @pytest.mark.asyncio
    async def test_rolls_back_state_on_alpaca_error(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Regression for review CRITICAL-3: when Alpaca rejects the
        limit-exit submission, we must NOT leave the position in CLOSING
        — fill detection has nothing to confirm and the row would leak
        forever. The in-memory _ActiveOrder must roll back to its prior
        status."""
        om = _make_om(config, tmp_db_path, mock_notifier)

        active = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            alpaca_target_order_id="target-1",
            status=PositionStatus.STOP_AND_TARGET_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
        )
        om._active_orders[1] = active
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("rejected"))

        order_id = await om.place_limit_exit(
            ticker="SPY", qty=10, limit_price=100.0, reason="time_stop",
        )

        assert order_id is None
        # Status MUST be back to its prior value, not stuck at CLOSING.
        assert active.status == PositionStatus.STOP_AND_TARGET_ACTIVE

    @pytest.mark.asyncio
    async def test_rejects_non_positive_qty(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        order_id = await om.place_limit_exit(
            ticker="SPY", qty=0, limit_price=100.0, reason="bug",
        )
        assert order_id is None

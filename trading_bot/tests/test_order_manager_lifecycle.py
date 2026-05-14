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

    def test_hydrate_join_returns_only_non_terminal_positions(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """H-2: the (positions LEFT JOIN trades) rewrite must materialise
        exactly the non-terminal positions and resolve db_trade_id from
        the matching trades row in one pass.

        Seeds a mix of CLOSED / ENTRY_FAILED / POSITION_OPEN /
        STOP_ACTIVE / ENTRY_PENDING positions and asserts:

        - terminal rows (CLOSED, ENTRY_FAILED) are excluded
        - non-terminal rows are present
        - each non-terminal row's db_trade_id points at the trades row
          that shares its (ticker, entry_time) pair
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Drop the per-position pending-trade-id cache so hydration is
        # forced to resolve db_trade_id via the JOIN.
        seeds = {
            "AAPL": PositionStatus.ENTRY_PENDING,
            "MSFT": PositionStatus.POSITION_OPEN,
            "GOOG": PositionStatus.STOP_ACTIVE,
            "TSLA": PositionStatus.CLOSED,
            "NVDA": PositionStatus.ENTRY_FAILED,
        }
        trade_ids: dict[str, int] = {}
        for ticker, status in seeds.items():
            tid = om._create_position_record(_entry(ticker))
            assert tid is not None
            trade_ids[ticker] = tid
            if status != PositionStatus.ENTRY_PENDING:
                om._update_position_status(tid, status)

        # Clear the in-memory cache so hydration cannot satisfy
        # db_trade_id from _pending_db_trade_ids — the JOIN must
        # populate it from the trades table.
        om._pending_db_trade_ids.clear()
        om._active_orders.clear()
        om._alpaca_to_trade.clear()

        om._hydrate_active_orders()

        live_tickers = {a.ticker for a in om._active_orders.values()}
        assert live_tickers == {"AAPL", "MSFT", "GOOG"}
        assert "TSLA" not in live_tickers
        assert "NVDA" not in live_tickers

        # Look up the trades.id for each non-terminal position and
        # assert the JOIN populated db_trade_id.
        conn = sqlite3.connect(tmp_db_path)
        try:
            for ticker in ("AAPL", "MSFT", "GOOG"):
                row = conn.execute(
                    "SELECT t.id "
                    "FROM positions p JOIN trades t "
                    "  ON t.ticker = p.ticker AND t.entry_time = p.entry_time "
                    "WHERE p.ticker = ?",
                    (ticker,),
                ).fetchone()
                expected_trade_id = int(row[0])
                tid = trade_ids[ticker]
                assert om._active_orders[tid].db_trade_id == expected_trade_id
        finally:
            conn.close()


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
        assert active.status == PositionStatus.STOP_ACTIVE
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
        assert active.status == PositionStatus.STOP_ACTIVE

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
        # Position transitioned to STOP_ACTIVE with standalone stop
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_ACTIVE
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
    async def test_timeout_with_broker_position_promotes_to_open(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Regression for 2026-05-11 XLK order-status lag bug.

        Alpaca filled the order in 169 ms but the bot's 5-min poll
        returned filled_qty=0 (stale endpoint). _maybe_timeout_entry
        was about to mark the row ENTRY_FAILED, which then orphaned the
        real broker-held position. The fix: before declaring failure,
        consult ``get_open_position`` — if the broker actually holds
        the qty, promote the row to POSITION_OPEN with the real fill
        data.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        entry_alpaca = _alpaca_order("entry-1", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        # Use integer-share entry so the _entry helper's int() doesn't
        # truncate to 0. The broker-side qty/price the consistency check
        # observes is what matters for the assertion.
        trade_id = await om.place_entry(_entry(shares=100, price=10.0))

        # Stale order status: still 'new', 0 filled, 10 min old (≥ default
        # entry_timeout). The broker, however, holds the position.
        old_submitted = datetime.now(tz=ET) - timedelta(seconds=600)
        stale_status = _alpaca_order(
            "entry-1", "new",
            filled_qty=0.0, filled_avg_price=0.0,
            submitted_at=old_submitted,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=stale_status)
        om._gw.client.cancel_order_by_id = MagicMock()
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("stop-1", "new"),
        )

        # Alpaca position list: position exists at the broker.
        broker_pos = MagicMock()
        broker_pos.qty = "100"  # str — matches alpaca-py serialization
        broker_pos.avg_entry_price = "10.00"
        om._gw.client.get_open_position = MagicMock(return_value=broker_pos)

        await om._check_order_statuses()

        active = om._active_orders[trade_id]
        # Promoted to POSITION_OPEN with the broker's qty + avg cost,
        # not ENTRY_FAILED — order-status lag must not orphan a real fill.
        assert active.status != PositionStatus.ENTRY_FAILED, (
            "broker holds the position; row must NOT be marked ENTRY_FAILED"
        )
        assert active.filled_shares == pytest.approx(100.0, abs=1e-6)
        assert active.entry_price == pytest.approx(10.0, abs=0.01)

    @pytest.mark.asyncio
    async def test_timeout_with_no_broker_position_marks_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Negative case: timeout fires, order-status is stale, AND the
        broker has no position — legitimate ENTRY_FAILED path.
        """
        from alpaca.common.exceptions import APIError

        om = _make_om(config, tmp_db_path, mock_notifier)
        entry_alpaca = _alpaca_order("entry-1", "new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        trade_id = await om.place_entry(_entry(shares=10, price=100.0))

        old_submitted = datetime.now(tz=ET) - timedelta(seconds=600)
        stale = _alpaca_order(
            "entry-1", "new",
            filled_qty=0.0, filled_avg_price=0.0,
            submitted_at=old_submitted,
        )
        om._gw.client.get_order_by_id = MagicMock(return_value=stale)
        om._gw.client.cancel_order_by_id = MagicMock()
        # Production Alpaca raises APIError when no position exists.
        om._gw.client.get_open_position = MagicMock(
            side_effect=APIError({"message": "position not found"}),
        )

        await om._check_order_statuses()

        assert trade_id not in om._active_orders, (
            "no broker position — legacy ENTRY_FAILED path must fire"
        )

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


class TestUninitiatedEntrySweep:
    """ENTRY_PENDING rows with alpaca_order_id IS NULL (crash between
    INSERT and submit_order) — the timeout sweep at line 257 requires a
    non-NULL order id, so without an explicit sweep these rows sit forever
    occupying a position slot. See risk_infrastructure_gaps.md item 4.
    """

    @pytest.mark.asyncio
    async def test_aged_pending_with_null_order_id_marked_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Pre-populate a ghost row directly: status=ENTRY_PENDING, no
        # alpaca_order_id, entry_time well past the timeout window.
        old_iso: str = (
            datetime.now(tz=ET) - timedelta(seconds=3600)
        ).isoformat()
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO positions (ticker, exchange, currency, "
                "quantity, entry_price, stop_price, target_price, "
                "status, hold_type, phase, strategy_id, entry_time, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "GHOST", "US", "USD", 100, 10.0, 9.8, 10.4,
                    PositionStatus.ENTRY_PENDING.value, "intraday", 1,
                    "mean_reversion", old_iso, old_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        # No Alpaca calls should be needed — the row has no order id.
        om._gw.client.get_order_by_id = MagicMock()

        await om._check_order_statuses()

        # Row is now ENTRY_FAILED in the DB and not in _active_orders.
        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE ticker = 'GHOST'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value
        # Should not have queried Alpaca for an order it never had.
        om._gw.client.get_order_by_id.assert_not_called()
        assert all(a.ticker != "GHOST" for a in om._active_orders.values())

    @pytest.mark.asyncio
    async def test_recent_pending_with_null_order_id_left_alone(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """A row with NULL order id but entry_time within the timeout
        window must NOT be marked ENTRY_FAILED — the submit could still
        be in flight (concurrent tick, slow Alpaca round trip).
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        recent_iso: str = datetime.now(tz=ET).isoformat()
        conn = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO positions (ticker, exchange, currency, "
                "quantity, entry_price, stop_price, target_price, "
                "status, hold_type, phase, strategy_id, entry_time, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "FRESH", "US", "USD", 100, 10.0, 9.8, 10.4,
                    PositionStatus.ENTRY_PENDING.value, "intraday", 1,
                    "mean_reversion", recent_iso, recent_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()

        await om._check_order_statuses()

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE ticker = 'FRESH'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_PENDING.value


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
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)

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
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)

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
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=100, filled_shares=100,
            entry_price=10.0, stop_price=9.8, target_price=10.4,
            trail_pct=0.02, trail_activation_price=10.20,
        )
        om._active_orders[trade_id] = active
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)

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
            status=PositionStatus.STOP_ACTIVE,
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
            status=PositionStatus.STOP_ACTIVE,
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
    async def test_place_exit_cancels_all_open_orders_including_phantom_stops(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Regression for 2026-05-11 XLK/XLF stuck-overnight bug.

        ``place_exit`` must cancel ALL open orders for the ticker, not
        just the locally-tracked bracket legs. When earlier retry paths
        place additional stops that aren't linked back into the DB,
        those phantom stops still hold the qty as ``held_for_orders``
        on Alpaca and cause the subsequent SELL to be rejected
        (Alpaca code 40310000 "insufficient qty available").

        See: trading_bot/execution/order_manager.py#place_exit.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",  # locally tracked
            alpaca_target_order_id="target-1",  # locally tracked
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        om._active_orders[1] = active

        # Alpaca reports three OPEN orders for SPY: the tracked bracket
        # legs PLUS a phantom stop the local DB doesn't know about.
        # All on the SELL side (bracket-leg sells) so the SELL-side
        # filter in place_exit catches them.
        from alpaca.trading.enums import OrderSide as _OrderSide

        def _mk_open(oid: str, side: str = "sell") -> Any:
            o = MagicMock()
            o.id = oid
            o.side = _OrderSide.SELL if side == "sell" else _OrderSide.BUY
            return o

        om._gw.client.get_orders = MagicMock(return_value=[
            _mk_open("stop-1"),
            _mk_open("target-1"),
            _mk_open("phantom-stop-from-retry"),
        ])

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
        # All three open orders cancelled — including the phantom stop
        assert "stop-1" in cancel_calls
        assert "target-1" in cancel_calls
        assert "phantom-stop-from-retry" in cancel_calls, (
            "phantom stop not in DB must still be cancelled — Alpaca is "
            "the source of truth for what's holding the qty"
        )
        # Market sell submitted exactly once after the cancels
        assert len(submit_orders) == 1
        # New order id mapped to the original trade id
        assert om._alpaca_to_trade.get("exit-1") == 1

    @pytest.mark.asyncio
    async def test_place_exit_preserves_cross_strategy_buy_entry(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Multi-strategy: if strategy B has an in-flight BUY entry on
        the same ticker, strategy A's place_exit must NOT cancel it.
        Only SELL-side orders (stops/targets/trailing/etc.) are
        candidates for cancellation, because only those reserve the
        qty that blocks A's SELL.
        """
        from alpaca.trading.enums import OrderSide as _OrderSide

        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1,
            ticker="XLF",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=50.0,
            stop_price=49.0,
            target_price=52.0,
            hold_type="intraday",
            strategy_id="overnight_drift",
        )
        om._active_orders[1] = active

        def _mk_open(oid: str, side: _OrderSide) -> Any:
            o = MagicMock()
            o.id = oid
            o.side = side
            return o

        # Alpaca reports: our SELL stop, plus another strategy's BUY
        # entry that just hasn't filled yet.
        om._gw.client.get_orders = MagicMock(return_value=[
            _mk_open("stop-1", _OrderSide.SELL),
            _mk_open("mean-reversion-buy-entry", _OrderSide.BUY),
        ])
        cancel_calls: list[str] = []
        om._gw.client.cancel_order_by_id = MagicMock(
            side_effect=lambda oid: cancel_calls.append(oid),
        )
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("exit-2", status="new"),
        )

        order_id = await om.place_exit(
            ticker="XLF", qty=10, reason="overnight_exit",
        )
        assert order_id == "exit-2"
        # SELL-side stop cancelled.
        assert "stop-1" in cancel_calls
        # Other strategy's BUY entry preserved.
        assert "mean-reversion-buy-entry" not in cancel_calls, (
            "cross-strategy BUY entry must not be collaterally "
            "cancelled — only SELL orders reserve our qty"
        )

    @pytest.mark.asyncio
    async def test_place_exit_side_filter_accepts_raw_string_side(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Defence-in-depth: if alpaca-py ever flattens OrderSide enums
        to raw strings in the response, the side-filter must still
        identify and cancel the matching SELL orders. Covers the
        isinstance(order_side, str) branch in cancel_all_for_ticker.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1,
            ticker="XLF",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=50.0,
            stop_price=49.0,
            target_price=52.0,
            hold_type="intraday",
            strategy_id="overnight_drift",
        )
        om._active_orders[1] = active

        # Raw-string sides, no .value attr (the future-SDK shape).
        def _mk_open(oid: str, side_str: str) -> Any:
            o = MagicMock()
            o.id = oid
            o.side = side_str
            return o

        om._gw.client.get_orders = MagicMock(return_value=[
            _mk_open("stop-1", "sell"),
            _mk_open("other-buy", "buy"),
        ])
        cancel_calls: list[str] = []
        om._gw.client.cancel_order_by_id = MagicMock(
            side_effect=lambda oid: cancel_calls.append(oid),
        )
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("exit-3", status="new"),
        )

        order_id = await om.place_exit(
            ticker="XLF", qty=10, reason="overnight_exit",
        )
        assert order_id == "exit-3"
        assert "stop-1" in cancel_calls, (
            "raw-string 'sell' side must match the filter and be cancelled"
        )
        assert "other-buy" not in cancel_calls, (
            "raw-string 'buy' side must not match a SELL filter"
        )

    @pytest.mark.asyncio
    async def test_place_exit_side_filter_skips_none_side(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """An order with no .side attribute (or .side=None) cannot be
        classified by side. The filter must skip it rather than nuke
        it — we'd rather leave an unidentifiable order alone than
        cancel something that might belong to a different concern.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1,
            ticker="XLF",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-known",
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=50.0,
            stop_price=49.0,
            target_price=52.0,
            hold_type="intraday",
            strategy_id="overnight_drift",
        )
        om._active_orders[1] = active

        from alpaca.trading.enums import OrderSide as _OrderSide

        def _mk_open_known_side(oid: str) -> Any:
            o = MagicMock()
            o.id = oid
            o.side = _OrderSide.SELL
            return o

        def _mk_open_no_side(oid: str) -> Any:
            o = MagicMock()
            o.id = oid
            o.side = None
            return o

        om._gw.client.get_orders = MagicMock(return_value=[
            _mk_open_known_side("stop-known"),
            _mk_open_no_side("malformed-no-side"),
        ])
        cancel_calls: list[str] = []
        om._gw.client.cancel_order_by_id = MagicMock(
            side_effect=lambda oid: cancel_calls.append(oid),
        )
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("exit-4", status="new"),
        )

        order_id = await om.place_exit(
            ticker="XLF", qty=10, reason="overnight_exit",
        )
        assert order_id == "exit-4"
        # Known SELL cancelled, unidentifiable order preserved.
        assert "stop-known" in cancel_calls
        assert "malformed-no-side" not in cancel_calls, (
            "order with .side=None must be skipped, not cancelled — "
            "we can't prove it's blocking our qty"
        )

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
    async def test_place_exit_notifies_on_non_emergency_failure(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Regression for 2026-05-08 → 2026-05-11 silent-failure mode.

        overnight_drift's morning exit failed daily with Alpaca's
        "insufficient qty available" rejection, but the alert path was
        gated on ``is_emergency=True`` — strategy callers pass False,
        so nothing ever fired. The position survived multiple sessions
        with zero operator visibility.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(side_effect=RuntimeError("rejected"))
        om._gw.client.get_orders = MagicMock(return_value=[])

        order_id = await om.place_exit(
            ticker="XLF", qty=1.989, reason="overnight_exit",
            is_emergency=False,
        )
        assert order_id is None
        mock_notifier.send.assert_awaited(), (
            "non-emergency exit failures must still notify — position is stuck"
        )

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
            status=PositionStatus.STOP_ACTIVE,
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
            status=PositionStatus.STOP_ACTIVE,
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
        assert active.status == PositionStatus.STOP_ACTIVE

    @pytest.mark.asyncio
    async def test_rejects_non_positive_qty(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        order_id = await om.place_limit_exit(
            ticker="SPY", qty=0, limit_price=100.0, reason="bug",
        )
        assert order_id is None

    @pytest.mark.asyncio
    async def test_place_limit_exit_cancels_phantom_stops(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Mirror of place_exit phantom-stop regression. The TAKE_PROFIT
        path is what finally cleared XLK on 2026-05-11 because the
        emergency-flatten fallback called cancel_all_for_ticker. With
        this fix the limit path itself cancels all open orders, so the
        fallback shouldn't be needed.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_stop_order_id="stop-1",  # locally tracked
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=100.0,
            stop_price=98.0,
            target_price=104.0,
            hold_type="intraday",
            strategy_id="mean_reversion",
        )
        om._active_orders[1] = active

        from alpaca.trading.enums import OrderSide as _OrderSide

        def _mk_open(oid: str, side: str = "sell") -> Any:
            o = MagicMock()
            o.id = oid
            o.side = _OrderSide.SELL if side == "sell" else _OrderSide.BUY
            return o

        om._gw.client.get_orders = MagicMock(return_value=[
            _mk_open("stop-1"),
            _mk_open("phantom-stop-from-retry"),
        ])
        cancel_calls: list[str] = []
        om._gw.client.cancel_order_by_id = MagicMock(
            side_effect=lambda oid: cancel_calls.append(oid),
        )
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("limit-exit-2", status="new"),
        )

        order_id = await om.place_limit_exit(
            ticker="SPY", qty=10, limit_price=104.5, reason="take_profit",
        )
        assert order_id == "limit-exit-2"
        assert "stop-1" in cancel_calls
        assert "phantom-stop-from-retry" in cancel_calls, (
            "limit-exit path must also cancel untracked stops so the "
            "SELL isn't rejected with insufficient-qty"
        )


# ---------------------------------------------------------------------------
# Strategy-exit fill callback (item #9 follow-up)
# ---------------------------------------------------------------------------


class TestStrategyExitFillCallback:
    """The OrderManager invokes ``exit_fill_callback`` when a strategy-driven
    exit FILLS — with the actual broker fill price, not the signal-time
    mid. The callback must NOT fire on cancel/expire/reject, since the
    position is left open and no realised P&L exists.
    """

    def _seed_closing_position(
        self, om: OrderManager, *, exit_oid: str = "exit-1",
        entry_price: float = 100.0, filled_shares: float = 10.0,
        strategy_id: str | None = "mean_reversion",
    ) -> _ActiveOrder:
        active = _ActiveOrder(
            trade_id=1,
            ticker="SPY",
            exchange="US",
            alpaca_entry_order_id="entry-1",
            alpaca_exit_order_id=exit_oid,
            status=PositionStatus.CLOSING,
            entry_shares=filled_shares,
            filled_shares=filled_shares,
            entry_price=entry_price,
            stop_price=entry_price * 0.98,
            target_price=entry_price * 1.04,
            hold_type="intraday",
            strategy_id=strategy_id,
            exit_reason="rsi_normalized",
        )
        om._active_orders[1] = active
        om._update_position_status(1, PositionStatus.CLOSING)
        return active

    @pytest.mark.asyncio
    async def test_callback_fires_with_actual_fill_price(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Signal-time mid was 95.00; broker filled at 94.85 (15c slippage)
        — callback must see the slippage-true P&L, not the signal-time one.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Seed a trades row so _close_position can update it.
        om._create_position_record(_entry("SPY"))
        active = self._seed_closing_position(om)
        active.db_trade_id = 1

        captured: list[tuple[str, str, float, float]] = []
        om.set_exit_fill_callback(
            lambda sid, tkr, qty, pnl: captured.append((sid, tkr, qty, pnl)),
        )

        # Broker fill at 94.85 — 15c worse than the signal mid 95.00.
        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order(
                "exit-1", "filled", filled_qty=10.0, filled_avg_price=94.85,
            ),
        )

        await om._check_order_statuses()

        assert active.status == PositionStatus.CLOSED
        assert len(captured) == 1
        sid, tkr, qty, pnl = captured[0]
        assert sid == "mean_reversion"
        assert tkr == "SPY"
        assert qty == 10.0
        # Real P&L = (94.85 - 100) * 10 = -51.50, not the -50.00 a
        # signal-time recording would have produced from mid=95.00.
        assert pnl == pytest.approx(-51.50, rel=1e-6)

    @pytest.mark.asyncio
    async def test_callback_not_fired_on_cancelled_exit(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """A limit exit that gets cancelled rolls back to
        STOP_ACTIVE — the position remains open, so no
        outcome is recorded.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = self._seed_closing_position(om)

        captured: list[Any] = []
        om.set_exit_fill_callback(lambda *args: captured.append(args))

        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order("exit-1", "canceled"),
        )

        await om._check_order_statuses()

        assert active.status == PositionStatus.STOP_ACTIVE
        assert captured == []

    @pytest.mark.asyncio
    async def test_callback_skipped_when_strategy_id_unknown(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """Drain/recovery exits without a sleeve label must not fire the
        cooldown callback — there's no strategy to charge the loss to.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._create_position_record(_entry("SPY"))
        active = self._seed_closing_position(om, strategy_id=None)
        active.db_trade_id = 1

        captured: list[Any] = []
        om.set_exit_fill_callback(lambda *args: captured.append(args))

        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order(
                "exit-1", "filled", filled_qty=10.0, filled_avg_price=99.0,
            ),
        )

        await om._check_order_statuses()
        assert active.status == PositionStatus.CLOSED
        assert captured == []

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_crash_status_poll(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """A misbehaving observer must not derail order-state reconciliation.
        The fill still completes; the callback failure is logged only.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._create_position_record(_entry("SPY"))
        active = self._seed_closing_position(om)
        active.db_trade_id = 1

        def boom(*_args):
            raise RuntimeError("tracker offline")

        om.set_exit_fill_callback(boom)

        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order(
                "exit-1", "filled", filled_qty=10.0, filled_avg_price=99.0,
            ),
        )

        await om._check_order_statuses()
        assert active.status == PositionStatus.CLOSED

    @pytest.mark.asyncio
    async def test_partial_fill_uses_broker_qty_for_callback_and_db(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """A partial-fill exit must report the same qty-true P&L to both
        the cooldown callback AND the trades-table row. Previously,
        ``_close_position`` recomputed using ``active.filled_shares``
        while the callback used ``exit_order.filled_qty`` — the two
        diverged on partial fills.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._create_position_record(_entry("SPY", shares=10, price=100.0))
        active = self._seed_closing_position(
            om, entry_price=100.0, filled_shares=10.0,
        )
        active.db_trade_id = 1

        captured: list[tuple[str, str, float, float]] = []
        om.set_exit_fill_callback(
            lambda sid, tkr, qty, pnl: captured.append((sid, tkr, qty, pnl)),
        )

        # Only 4 of 10 shares fill at 95.00 — the position closes but
        # the realised loss is bounded by the partial qty.
        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order(
                "exit-1", "filled", filled_qty=4.0, filled_avg_price=95.0,
            ),
        )

        await om._check_order_statuses()

        # Callback sees the broker-reported qty (4), not the entry qty (10).
        assert len(captured) == 1
        _sid, _tkr, qty, pnl = captured[0]
        assert qty == 4.0
        assert pnl == pytest.approx(-20.0, rel=1e-6)  # (95 - 100) * 4

        # And the trades row was written with the SAME qty-true P&L.
        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT gross_pnl, net_pnl, exit_price FROM trades WHERE id = 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        gross_pnl, net_pnl, exit_price = row
        assert gross_pnl == pytest.approx(-20.0, rel=1e-6)
        assert net_pnl == pytest.approx(-20.0, rel=1e-6)
        assert exit_price == pytest.approx(95.0, rel=1e-6)

    @pytest.mark.asyncio
    async def test_callback_fires_via_place_limit_exit_path(
        self, config, tmp_db_path: str, mock_notifier,
    ):
        """``place_limit_exit`` transitions to CLOSING and sets
        ``alpaca_exit_order_id`` the same way ``place_exit`` does, so
        the same _check_order_statuses fill branch handles both. This
        test seeds the position via the real ``place_limit_exit`` call
        to guard against future drift between the two paths.
        """
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Real entry → real DB row → real trades row, so _close_position
        # has something to update.
        om._create_position_record(_entry("SPY"))
        active = _ActiveOrder(
            trade_id=1, ticker="SPY", exchange="US",
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.STOP_ACTIVE,
            entry_shares=10.0, filled_shares=10.0,
            entry_price=100.0, stop_price=98.0, target_price=104.0,
            hold_type="intraday", strategy_id="mean_reversion",
            db_trade_id=1,
        )
        om._active_orders[1] = active

        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("exit-limit-1", "new"),
        )
        order_id = await om.place_limit_exit(
            ticker="SPY", qty=10, limit_price=99.50, reason="time_stop",
        )
        assert order_id == "exit-limit-1"
        assert active.status == PositionStatus.CLOSING

        captured: list[tuple[str, str, float, float]] = []
        om.set_exit_fill_callback(
            lambda sid, tkr, qty, pnl: captured.append((sid, tkr, qty, pnl)),
        )

        om._gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order(
                "exit-limit-1", "filled",
                filled_qty=10.0, filled_avg_price=99.40,
            ),
        )
        await om._check_order_statuses()

        assert active.status == PositionStatus.CLOSED
        assert len(captured) == 1
        sid, _tkr, qty, pnl = captured[0]
        assert sid == "mean_reversion"
        assert qty == 10.0
        assert pnl == pytest.approx(-6.0, rel=1e-6)  # (99.40 - 100) * 10


# ---------------------------------------------------------------------------
# _coerce_broker_qty — defensive coercion helper for the timeout fallback
# ---------------------------------------------------------------------------


class TestCoerceBrokerQty:
    """Locks in the strict-type contract for the broker-position fallback.

    The helper is the gatekeeper between Alpaca position fields and the
    promote-to-open path in _maybe_timeout_entry. Loose coercion here
    would cause silent miscategorisation of a held position vs no
    position, which directly affects whether a row becomes
    POSITION_OPEN or ENTRY_FAILED.
    """

    def test_accepts_alpaca_string_qty(self) -> None:
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty("0.3927") == pytest.approx(0.3927)
        assert _coerce_broker_qty("100") == 100.0

    def test_accepts_int_and_float(self) -> None:
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty(10) == 10.0
        assert _coerce_broker_qty(3.5) == 3.5

    def test_rejects_none(self) -> None:
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty(None) is None

    def test_rejects_bool(self) -> None:
        """bool is a subclass of int — explicit reject so float(True)==1.0
        can't silently look like a 1-share position."""
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty(True) is None
        assert _coerce_broker_qty(False) is None

    def test_rejects_magicmock(self) -> None:
        """Defends against test gateways that auto-mock the qty attribute.
        Without this guard, float(MagicMock()) returns 1.0 and the
        timeout-fallback path would falsely report a held position."""
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty(MagicMock()) is None

    def test_rejects_empty_string(self) -> None:
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty("") is None
        assert _coerce_broker_qty("   ") is None

    def test_rejects_garbage_string(self) -> None:
        from trading_bot.execution.order_manager import _coerce_broker_qty
        assert _coerce_broker_qty("not-a-number") is None

"""Regression tests for the issue #117 standalone-stop lifecycle gaps.

Covers three failure modes that left fractional positions naked at Alpaca:

A. Stop cancelled at the broker but DB still references the dead order id —
   pre-#117 the stop-poll loop ignored ``canceled`` / ``expired`` / ``rejected``
   statuses, so the row sat in ``STOP_ACTIVE`` forever.
B. ``_transition_to_open`` interrupted between the status flip and the
   stop-id write — the row landed in ``POSITION_OPEN`` with
   ``alpaca_stop_order_id=NULL`` and no branch in ``_check_order_statuses``
   handled it. Self-heal only happened on the next market-open
   ``_verify_stop_orders`` sweep.
C. Overnight_drift entries at 15:45 ET produced the same B-shaped state
   on the next tick.

The fix adds a stop-cancellation handler (A) that demotes the row to
``POSITION_OPEN`` and a recovery branch (B/C) that re-attaches the
standalone stop during market hours.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.execution.order_manager import (
    EntryDecision,
    OrderManager,
)
from trading_bot.execution.stop_reconciler import (
    reconcile_open_position_stops,
)

pytestmark = pytest.mark.critical

ET = TZ_EASTERN


# ---------------------------------------------------------------------------
# Helpers (mirrors test_order_manager_lifecycle helpers, kept local to
# avoid coupling regression coverage to refactors of that file)
# ---------------------------------------------------------------------------


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _alpaca_order(
    order_id: str = "order-1",
    status: str = "new",
    filled_qty: float = 0.0,
    filled_avg_price: float = 0.0,
):
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    return o


def _entry(
    ticker: str = "XLI",
    shares: float = 0.3181,
    price: float = 173.008,
    strategy_id: str = "mean_reversion",
    hold_type: str = "intraday",
) -> EntryDecision:
    return EntryDecision(
        ticker=ticker,
        exchange="US",
        side="BUY",
        shares=shares,
        limit_price=price,
        stop_price=round(price * 0.98, 2),
        target_price=round(price * 1.02, 2),
        hold_type=hold_type,
        sector="Industrials",
        phase=3,
        sentiment_score=0.0,
        signals="test",
        currency="USD",
        strategy_id=strategy_id,
        trail_pct=None,
        trail_activation_price=None,
    )


def _set_market_open(om: OrderManager) -> None:
    """Make ``_inside_market_hours_for_stop_attach`` return True via the
    Alpaca clock path. Uses a fixed forward-dated ``next_close`` so the
    5-minute buffer check passes regardless of when the suite runs.
    """
    clock = MagicMock()
    clock.is_open = True
    clock.next_close = datetime.now(tz=ET) + timedelta(hours=1)
    om._gw.client.get_clock = MagicMock(return_value=clock)


def _set_market_closed(om: OrderManager) -> None:
    clock = MagicMock()
    clock.is_open = False
    clock.next_close = None
    om._gw.client.get_clock = MagicMock(return_value=clock)


# ---------------------------------------------------------------------------
# Failure mode B — POSITION_OPEN with NULL stop_order_id (mid-tick crash)
# ---------------------------------------------------------------------------


class TestFailureModeB_StopNeverAttached:
    """``_transition_to_open`` interrupted mid-flow.

    Reproduces XLI #99 (2026-05-12 11:45 ET): mean_reversion entry filled,
    POSITION_OPEN landed in the DB, but ``_place_standalone_stop`` never
    persisted an order id. Pre-fix the next tick simply hydrated this
    state and fell through every branch, leaving the position naked.
    """

    @pytest.mark.asyncio
    async def test_recovery_attaches_fresh_stop(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        # Simulate the post-crash DB state: POSITION_OPEN, no stop id.
        trade_id = om._create_position_record(_entry("XLI"))
        om._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        # Fill quantity so the recovery branch's guard passes.
        om._update_position_field(trade_id, "quantity", 0.3181)
        om._update_position_field(trade_id, "entry_price", 173.008)

        # Alpaca has no existing matching stop, so recovery falls through
        # to submitting a fresh one. Pre-fix path: no submit ever fires.
        om._gw.client.get_orders = MagicMock(return_value=[])
        captured: list = []

        def _submit(order_data):
            captured.append(order_data)
            return _alpaca_order(f"stop-recovered-{len(captured)}", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)

        await om._check_order_statuses()

        assert len(captured) == 1, (
            "expected exactly one stop submission; pre-fix the recovery "
            "branch did not exist so this would be zero"
        )

        # Status promoted and order id persisted.
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_ACTIVE
        assert active.alpaca_stop_order_id == "stop-recovered-1"

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status, alpaca_stop_order_id FROM positions "
                "WHERE id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.STOP_ACTIVE.value
        assert row[1] == "stop-recovered-1"

    @pytest.mark.asyncio
    async def test_recovery_adopts_existing_broker_stop(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """Submit-response loss: a prior tick's submit reached Alpaca but
        the SDK raised during response parsing, so the DB never got the
        order id. Recovery should adopt the live broker stop rather than
        submitting a second one that would hold double the qty."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        trade_id = om._create_position_record(_entry("XLI"))
        om._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        om._update_position_field(trade_id, "quantity", 0.3181)
        om._update_position_field(trade_id, "entry_price", 173.008)

        # Live matching stop on the book — sell-side, matching qty + price.
        live_stop = MagicMock()
        live_stop.id = "stop-on-broker"
        live_stop.order_type = MagicMock(value="stop")
        live_stop.type = MagicMock(value="stop")
        live_stop.side = MagicMock(value="sell")
        live_stop.qty = "0.3181"
        live_stop.stop_price = round(173.008 * 0.98, 2)
        om._gw.client.get_orders = MagicMock(return_value=[live_stop])

        # Any submit_order call would be a bug.
        om._gw.client.submit_order = MagicMock(
            side_effect=AssertionError("must not submit a second stop"),
        )

        await om._check_order_statuses()

        active = om._active_orders[trade_id]
        assert active.alpaca_stop_order_id == "stop-on-broker"
        assert active.status == PositionStatus.STOP_ACTIVE


# ---------------------------------------------------------------------------
# Failure mode C — overnight_drift 15:45 ET entry, no stop next tick
# ---------------------------------------------------------------------------


class TestFailureModeC_OvernightDriftEntry:
    """End-to-end: overnight_drift entry fills, next tick processes the
    fill and must attach a stop. Pre-#117 there's a narrow path where
    the post-fill stop-attach silently drops; the recovery branch is
    the safety net for the next tick.
    """

    @pytest.mark.asyncio
    async def test_late_session_entry_recovers_stop_next_tick(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        # Simulate XLK #102 / XLF #103 (2026-05-13 15:45 ET): entry filled,
        # row landed POSITION_OPEN without a stop id.
        trade_id = om._create_position_record(
            _entry("XLK", shares=0.5735, price=177.368, strategy_id="overnight_drift", hold_type="swing"),
        )
        om._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        om._update_position_field(trade_id, "quantity", 0.5735)
        om._update_position_field(trade_id, "entry_price", 177.368)

        om._gw.client.get_orders = MagicMock(return_value=[])
        submits: list = []

        def _submit(order_data):
            submits.append(order_data)
            return _alpaca_order(f"stop-{len(submits)}", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)

        await om._check_order_statuses()

        assert len(submits) == 1
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_ACTIVE
        assert active.alpaca_stop_order_id == "stop-1"

    @pytest.mark.asyncio
    async def test_recovery_deferred_when_market_closed(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """Outside the safe placement window, the recovery defers to the
        next-market-open ``_verify_stop_orders`` sweep — submitting a
        DAY stop after close would be deferred to next session and held
        as a phantom (the bug PR #102 fixed for recovery)."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_closed(om)

        trade_id = om._create_position_record(_entry("XLK"))
        om._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        om._update_position_field(trade_id, "quantity", 0.5735)

        # If recovery does submit, this assertion fails the test.
        om._gw.client.submit_order = MagicMock(
            side_effect=AssertionError("must not submit outside window"),
        )
        om._gw.client.get_orders = MagicMock(return_value=[])

        await om._check_order_statuses()

        # Row stays POSITION_OPEN; the next market-open recovery sweep
        # is the ultimate fallback. The submit_order side_effect would
        # have fired if recovery tried — its absence is the assertion.
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.POSITION_OPEN
        assert active.alpaca_stop_order_id is None


# ---------------------------------------------------------------------------
# Failure mode A — stop cancelled at broker, DB unaware
# ---------------------------------------------------------------------------


class TestFailureModeA_StopCancelledAtBroker:
    """Reproduces SPY #90 / QQQ #91 (2026-05-07): stops were attached
    correctly at entry but cancelled at 16:07 ET. Pre-#117 the stop-poll
    loop only handled ``filled`` status; ``canceled`` was a no-op, so
    the row sat in ``STOP_ACTIVE`` with a dead order id and
    no protective stop on the book.
    """

    @pytest.mark.asyncio
    async def test_cancelled_stop_demotes_to_position_open(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        trade_id = om._create_position_record(_entry("SPY", shares=0.1681, price=732.976))
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
        om._update_position_field(trade_id, "quantity", 0.1681)
        om._update_position_field(trade_id, "entry_price", 732.976)
        om._update_position_field(trade_id, "alpaca_stop_order_id", "stop-dead")

        # Stop-poll: stop returns canceled. Recovery branch then runs and
        # finds no existing broker stop, so it submits a fresh one.
        submits: list = []

        def _submit(order_data):
            submits.append(order_data)
            return _alpaca_order(f"stop-fresh-{len(submits)}", "new")

        om._gw.client.submit_order = MagicMock(side_effect=_submit)
        om._gw.client.get_orders = MagicMock(return_value=[])

        def _get_order_by_id(oid: str):
            if oid == "stop-dead":
                return _alpaca_order(oid, "canceled")
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=_get_order_by_id)

        await om._check_order_statuses()

        # Fresh stop attached on the same tick.
        assert len(submits) == 1
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.STOP_ACTIVE
        assert active.alpaca_stop_order_id == "stop-fresh-1"

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status, alpaca_stop_order_id FROM positions "
                "WHERE id = ?",
                (trade_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.STOP_ACTIVE.value
        assert row[1] == "stop-fresh-1"

    @pytest.mark.asyncio
    async def test_cancelled_target_does_not_demote(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """Only stop cancellation triggers the POSITION_OPEN demotion.
        A cancelled take-profit (target) leg clears its own id but
        leaves status untouched — the protective stop is still on the
        book and the position is not naked."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        trade_id = om._create_position_record(_entry("SPY"))
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
        om._update_position_field(trade_id, "quantity", 100)
        om._update_position_field(trade_id, "alpaca_stop_order_id", "stop-live")
        om._update_position_field(trade_id, "alpaca_target_order_id", "target-dead")

        def _get_order_by_id(oid: str):
            if oid == "stop-live":
                return _alpaca_order(oid, "new")
            if oid == "target-dead":
                return _alpaca_order(oid, "canceled")
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=_get_order_by_id)
        om._gw.client.get_orders = MagicMock(return_value=[])

        await om._check_order_statuses()

        active = om._active_orders[trade_id]
        # Status preserved — stop is still active.
        assert active.status == PositionStatus.STOP_ACTIVE
        # Dead target id cleared.
        assert active.alpaca_target_order_id is None
        assert active.alpaca_stop_order_id == "stop-live"

    @pytest.mark.asyncio
    async def test_cancelled_stop_breaks_loop_does_not_double_process(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """After the stop cancellation handler demotes to POSITION_OPEN,
        the leg loop must break — otherwise remaining legs (target,
        trail) are polled against a row whose status was just changed,
        producing confusing logs. Verifies the post-review break fix."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        trade_id = om._create_position_record(_entry("SPY"))
        om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
        om._update_position_field(trade_id, "quantity", 100)
        om._update_position_field(trade_id, "alpaca_stop_order_id", "stop-dead")
        om._update_position_field(trade_id, "alpaca_target_order_id", "target-live")

        get_calls: list[str] = []

        def _get_order_by_id(oid: str):
            get_calls.append(oid)
            if oid == "stop-dead":
                return _alpaca_order(oid, "canceled")
            if oid == "target-live":
                return _alpaca_order(oid, "new")
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=_get_order_by_id)
        # Make _find_existing_stop return no match so recovery submits fresh.
        om._gw.client.get_orders = MagicMock(return_value=[])
        om._gw.client.submit_order = MagicMock(
            return_value=_alpaca_order("stop-fresh", "new"),
        )

        await om._check_order_statuses()

        # The target leg must NOT have been queried after the stop's
        # cancellation handler demoted the row — break exits the loop.
        assert "stop-dead" in get_calls
        assert "target-live" not in get_calls

    @pytest.mark.asyncio
    async def test_cancelled_trail_demotes_to_stop_and_target_active(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """TRAILING_ACTIVE rows keep ``alpaca_stop_order_id`` populated
        (activate_trailing_stop only cancels the take-profit leg, not
        the protective stop). A cancelled trail order therefore is NOT
        a naked-position event — but the row mustn't get stuck in
        TRAILING_ACTIVE with a NULL trail id, or check_trail_activations
        will never re-fire. Demoting to STOP_ACTIVE clears
        the trail-activated flag so re-activation can run on the next
        price cross."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        trade_id = om._create_position_record(_entry("SPY"))
        om._update_position_status(trade_id, PositionStatus.TRAILING_ACTIVE)
        om._update_position_field(trade_id, "quantity", 100)
        om._update_position_field(trade_id, "alpaca_stop_order_id", "stop-live")
        om._update_position_field(trade_id, "alpaca_trail_order_id", "trail-dead")
        # Mark trail_activated so we can verify the demotion clears it.
        # (Activation flag is in-memory only on _ActiveOrder; hydrate
        # manually for the test.)

        def _get_order_by_id(oid: str):
            if oid == "stop-live":
                return _alpaca_order(oid, "new")
            if oid == "trail-dead":
                return _alpaca_order(oid, "canceled")
            return _alpaca_order(oid, "new")

        om._gw.client.get_order_by_id = MagicMock(side_effect=_get_order_by_id)
        om._gw.client.get_orders = MagicMock(return_value=[])

        # Hydrate to load the row, then pre-set trail_activated.
        om._hydrate_active_orders()
        om._active_orders[trade_id].trail_activated = True

        await om._check_order_statuses()

        active = om._active_orders[trade_id]
        # Demoted, not stuck in TRAILING_ACTIVE.
        assert active.status == PositionStatus.STOP_ACTIVE
        assert active.alpaca_trail_order_id is None
        # Fixed stop still in place — position is not naked.
        assert active.alpaca_stop_order_id == "stop-live"
        # Re-activation flag reset so trail can re-fire next price cross.
        assert active.trail_activated is False


class TestRecoveryGuardEdgeCases:
    """Edge cases around the new POSITION_OPEN-without-stop recovery branch."""

    @pytest.mark.asyncio
    async def test_recovery_skipped_when_stop_price_zero(
        self, config, tmp_db_path: str, mock_notifier, caplog
    ) -> None:
        """A row with stop_price=0 cannot have a stop attached — the
        underlying _place_standalone_stop refuses non-positive prices.
        The recovery guard must skip with a warning rather than silently
        falling through (operator needs visibility into upstream bugs)."""
        import logging
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)

        # Seed: POSITION_OPEN, no stop id, qty > 0, but stop_price = 0.
        entry = _entry("XLI")
        entry.stop_price = 0.0  # type: ignore[misc]
        trade_id = om._create_position_record(entry)
        om._update_position_status(trade_id, PositionStatus.POSITION_OPEN)
        om._update_position_field(trade_id, "quantity", 0.5)
        om._update_position_field(trade_id, "stop_price", 0.0)

        om._gw.client.submit_order = MagicMock(
            side_effect=AssertionError("must not submit with stop_price=0"),
        )

        with caplog.at_level(logging.WARNING):
            await om._check_order_statuses()

        # Recovery skipped, row left for operator inspection.
        active = om._active_orders[trade_id]
        assert active.status == PositionStatus.POSITION_OPEN
        assert active.alpaca_stop_order_id is None
        # The warning surfaces the upstream bug.
        assert any(
            "non-positive" in record.message for record in caplog.records
        )


# ---------------------------------------------------------------------------
# Reconciliation report
# ---------------------------------------------------------------------------


class TestStopReconciliation:
    """The reconciler must surface naked positions even when in-tick
    recovery silently heals them — that's how the operator learns the
    underlying drop-the-stop bug occurred at all."""

    def _seed_open_position(
        self,
        db_path: str,
        ticker: str,
        status: PositionStatus,
        stop_order_id: str | None,
        *,
        strategy_id: str = "mean_reversion",
        entry_time: datetime | None = None,
    ) -> int:
        conn = sqlite3.connect(db_path)
        try:
            entry_iso: str = (
                entry_time.isoformat() if entry_time is not None
                else datetime.now(tz=ET).isoformat()
            )
            now: str = datetime.now(tz=ET).isoformat()
            cur = conn.execute(
                "INSERT INTO positions "
                "(ticker, exchange, currency, sector, quantity, entry_price, "
                " entry_time, status, stop_price, target_price, hold_type, "
                " phase, strategy_id, highest_price, updated_at, "
                " alpaca_stop_order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ticker, "US", "USD", "Industrials",
                    0.5, 100.0, entry_iso, status.value,
                    98.0, 102.0, "intraday",
                    3, strategy_id, 100.0, now,
                    stop_order_id,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_naked_position_with_null_stop_id_is_reported(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        self._seed_open_position(
            tmp_db_path, "XLI", PositionStatus.POSITION_OPEN, None,
        )

        gw = MagicMock()
        gw.client = MagicMock()

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert result.has_naked
        assert len(result.naked) == 1
        assert result.naked[0].ticker == "XLI"
        assert result.naked[0].broker_status == "missing"
        mock_notifier.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_naked_position_with_cancelled_stop_is_reported(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        self._seed_open_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE,
            "stop-cancelled",
        )

        gw = MagicMock()
        gw.client = MagicMock()
        gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order("stop-cancelled", "canceled"),
        )

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert result.has_naked
        assert result.naked[0].ticker == "SPY"
        assert result.naked[0].broker_status == "canceled"

    @pytest.mark.asyncio
    async def test_position_with_active_stop_not_reported(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        self._seed_open_position(
            tmp_db_path, "XLK", PositionStatus.STOP_ACTIVE,
            "stop-healthy",
        )

        gw = MagicMock()
        gw.client = MagicMock()
        gw.client.get_order_by_id = MagicMock(
            return_value=_alpaca_order("stop-healthy", "new"),
        )

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert not result.has_naked
        assert result.rows_checked == 1
        mock_notifier.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_alpaca_lookup_failure_treated_as_missing(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """A transient Alpaca outage during reconciliation should err on
        the side of surfacing the position (better a false alarm than a
        silent naked position)."""
        self._seed_open_position(
            tmp_db_path, "XLF", PositionStatus.STOP_ACTIVE,
            "stop-flaky",
        )

        gw = MagicMock()
        gw.client = MagicMock()
        gw.client.get_order_by_id = MagicMock(
            side_effect=RuntimeError("alpaca 503"),
        )

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert result.has_naked
        assert result.naked[0].broker_status == "missing"

    # ------------------------------------------------------------------
    # Issue #129: strategy-aware notification suppression for the
    # legitimate overnight_drift entry → next-tick stop-attach window.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_reconciler_notifies_naked_mean_reversion(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Baseline: mean_reversion entries have no grace window — a
        naked row fires the priority-4 notification regardless of how
        recent the entry is."""
        self._seed_open_position(
            tmp_db_path, "XLI", PositionStatus.POSITION_OPEN, None,
            strategy_id="mean_reversion",
            entry_time=datetime.now(tz=ET) - timedelta(minutes=2),
        )

        gw = MagicMock()
        gw.client = MagicMock()

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert result.has_naked
        mock_notifier.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_reconciler_suppresses_overnight_drift_in_grace_window(
        self, tmp_db_path: str, mock_notifier, caplog
    ) -> None:
        """An overnight_drift entry timestamped within the 10-minute
        grace window is detected and logged but does NOT fire the push
        notification — that's the operator-fatigue mitigation from
        issue #129."""
        import logging
        self._seed_open_position(
            tmp_db_path, "XLK", PositionStatus.POSITION_OPEN, None,
            strategy_id="overnight_drift",
            entry_time=datetime.now(tz=ET) - timedelta(minutes=2),
        )

        gw = MagicMock()
        gw.client = MagicMock()

        with caplog.at_level(logging.WARNING):
            result = await reconcile_open_position_stops(
                db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
            )

        # Detection still happens — emergency stop-attach path still fires.
        assert result.has_naked
        assert result.naked[0].strategy_id == "overnight_drift"
        # Notification suppressed.
        mock_notifier.send.assert_not_awaited()
        # Operator can grep the log for the suppressed event.
        assert any(
            "suppressing notification" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_reconciler_notifies_overnight_drift_past_grace_window(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """An overnight_drift entry older than the grace window is a
        real failure of the stop-attach path — both log and
        notification must fire. Guards against silently masking the
        very class of bug the reconciler exists to catch (#117 mode C)."""
        self._seed_open_position(
            tmp_db_path, "XLF", PositionStatus.POSITION_OPEN, None,
            strategy_id="overnight_drift",
            entry_time=datetime.now(tz=ET) - timedelta(minutes=30),
        )

        gw = MagicMock()
        gw.client = MagicMock()

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert result.has_naked
        mock_notifier.send.assert_awaited()

    @pytest.mark.asyncio
    async def test_reconciler_mixed_batch_notifies_only_non_grace(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """When one overnight_drift row is in grace and one
        mean_reversion row is naked, exactly one notification fires and
        its body excludes the suppressed row."""
        self._seed_open_position(
            tmp_db_path, "XLK", PositionStatus.POSITION_OPEN, None,
            strategy_id="overnight_drift",
            entry_time=datetime.now(tz=ET) - timedelta(minutes=2),
        )
        self._seed_open_position(
            tmp_db_path, "XLI", PositionStatus.POSITION_OPEN, None,
            strategy_id="mean_reversion",
            entry_time=datetime.now(tz=ET) - timedelta(minutes=2),
        )

        gw = MagicMock()
        gw.client = MagicMock()

        result = await reconcile_open_position_stops(
            db_path=tmp_db_path, gateway=gw, notifier=mock_notifier,
        )

        assert len(result.naked) == 2
        mock_notifier.send.assert_awaited_once()
        # Inspect the body — only the mean_reversion ticker should appear.
        call_args = mock_notifier.send.await_args
        body: str = call_args.args[1]
        assert "XLI" in body
        assert "XLK" not in body


# ---------------------------------------------------------------------------
# Market-hours gate
# ---------------------------------------------------------------------------


class TestStopRecoveryMarketHoursGate:

    @pytest.mark.asyncio
    async def test_clock_closed_defers_recovery(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_closed(om)
        assert not await om._inside_market_hours_for_stop_attach()

    @pytest.mark.asyncio
    async def test_clock_open_with_long_runway_allows_recovery(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _set_market_open(om)
        assert await om._inside_market_hours_for_stop_attach()

    @pytest.mark.asyncio
    async def test_clock_open_close_to_bell_defers(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        """4 minutes from close — DAY stop would be deferred to next
        session, so the recovery should skip rather than create the
        phantom-stop class of bug PR #102 fixed for recovery."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        clock = MagicMock()
        clock.is_open = True
        clock.next_close = datetime.now(tz=ET) + timedelta(minutes=4)
        om._gw.client.get_clock = MagicMock(return_value=clock)
        assert not await om._inside_market_hours_for_stop_attach()

    @pytest.mark.asyncio
    async def test_clock_unavailable_falls_back_to_time_gate(
        self, config, tmp_db_path: str, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.get_clock = MagicMock(
            side_effect=RuntimeError("clock 503"),
        )
        # Result depends on wall-clock time; just verify no crash.
        result = await om._inside_market_hours_for_stop_attach()
        assert isinstance(result, bool)

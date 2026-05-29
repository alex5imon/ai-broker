"""Tests for StateRecovery (startup reconciliation)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recovery(
    gateway,
    db_path: str,
    notifier,
    now: datetime | None = None,
    config: dict[str, Any] | None = None,
) -> Any:
    from trading_bot.gateway.recovery import StateRecovery
    return StateRecovery(
        gateway=gateway,
        db_path=db_path,
        notifier=notifier,
        config=config or {"exit_intraday": {"stop_loss_pct": 0.02}},
        now_fn=(lambda: now) if now is not None else None,
    )


def _alpaca_position(
    ticker: str,
    qty: float = 100.0,
    avg_entry_price: float = 10.0,
    exchange: str = "NASDAQ",
) -> MagicMock:
    pos = MagicMock()
    pos.symbol = ticker
    pos.qty = str(qty)
    pos.avg_entry_price = str(avg_entry_price)
    pos.exchange = exchange
    return pos


def _alpaca_order(
    ticker: str,
    order_type: str = "stop",
    submitted_at: datetime | None = None,
    order_id: str = "ord-1",
) -> MagicMock:
    order = MagicMock()
    order.symbol = ticker
    order.type = MagicMock()
    order.type.value = order_type
    order.id = order_id
    order.submitted_at = submitted_at
    order.created_at = submitted_at
    return order


def _insert_db_position_intraday(
    conn: sqlite3.Connection,
    ticker: str,
    qty: int = 100,
    entry_price: float = 10.0,
) -> int:
    cur = conn.execute(
        """INSERT INTO positions
           (ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ticker, "NASDAQ", "USD", qty, entry_price,
         datetime.now(ET).isoformat(), "POSITION_OPEN", "intraday", 1, "mr"),
    )
    conn.commit()
    return cur.lastrowid


def _insert_db_position(
    conn: sqlite3.Connection,
    ticker: str,
    qty: int = 100,
    entry_price: float = 10.0,
    status: str = "POSITION_OPEN",
) -> int:
    cur = conn.execute(
        """INSERT INTO positions
           (ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ticker, "NASDAQ", "USD", qty, entry_price,
         datetime.now(ET).isoformat(), status, "swing", 1, "unknown"),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Reconciliation logic
# ---------------------------------------------------------------------------


class TestStateRecovery:
    def _make_gateway(
        self,
        positions: list | None = None,
        orders: list | None = None,
        account: dict | None = None,
    ) -> MagicMock:
        gw = MagicMock()
        gw.account_id = "TEST_ACCOUNT"
        gw.client = MagicMock()
        gw.get_positions = AsyncMock(return_value=positions or [])
        gw.get_open_orders = AsyncMock(return_value=orders or [])
        gw.get_account_summary = AsyncMock(
            return_value=account
            or {
                "NetLiquidation": "1000.0",
                "SettledCash": "950.0",
                "BuyingPower": "950.0",
            }
        )
        return gw

    @pytest.mark.asyncio
    async def test_ib_position_not_in_db_created(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Alpaca has PLTR, DB doesn't → DB record created."""
        pos = _alpaca_position("PLTR", qty=100)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()
        assert "PLTR" in result.positions_created

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT * FROM positions WHERE ticker='PLTR'"
        ).fetchone()
        conn.close()
        assert row is not None

    @pytest.mark.asyncio
    async def test_db_position_not_in_ib_closed(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """DB has PLTR, Alpaca doesn't → DB record marked CLOSED."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "PLTR")
        conn.close()

        gw = self._make_gateway(positions=[])
        # Pin the clock 1 hour in the future so PLTR's just-inserted
        # entry_time is far outside the fresh-entry grace window.
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()
        assert "PLTR" in result.positions_closed_mismatch

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE ticker='PLTR'"
        ).fetchone()
        conn.close()
        assert row[0] == "CLOSED"

    @pytest.mark.asyncio
    async def test_db_position_not_in_alpaca_deferred_when_entry_fresh(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Live bug observed 2026-05-06: bot opens MSFT at 09:55 ET, the
        next 10:00 tick's reconciler fires before Alpaca's positions
        endpoint reflects the fill, and the row is closed as
        reconciliation_mismatch with NULL exit data — phantom round-trip.

        Fix: positions whose entry_time is within the configured grace
        window must be deferred, not closed. The next tick (outside the
        window) will either see the position on Alpaca or close it for
        real if it never appeared.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "MSFT")  # entry_time = now
        conn.close()

        gw = self._make_gateway(positions=[])
        # 5 minutes after entry — well within the 10-min default grace.
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(minutes=5),
        )
        result = await recovery.recover()

        assert "MSFT" in result.positions_deferred_fresh, (
            "fresh entry must be deferred, not silently closed"
        )
        assert "MSFT" not in result.positions_closed_mismatch

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE ticker='MSFT'"
        ).fetchone()
        conn.close()
        assert row[0] == "POSITION_OPEN", (
            "deferred row must remain POSITION_OPEN — closing it would "
            "create the same phantom round-trip the fix is preventing."
        )

    @pytest.mark.asyncio
    async def test_db_position_grace_window_overridable_via_config(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Setting recovery.fresh_entry_grace_minutes to 0 disables the
        defer (covers the regression-tightening case where an operator
        wants the original behavior back temporarily)."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "PLTR")
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(minutes=2),
            config={
                "exit_intraday": {"stop_loss_pct": 0.02},
                "recovery": {"fresh_entry_grace_minutes": 0},
            },
        )
        result = await recovery.recover()
        assert "PLTR" in result.positions_closed_mismatch
        assert "PLTR" not in result.positions_deferred_fresh

    @pytest.mark.asyncio
    async def test_db_position_closing_with_exit_order_deferred(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Regression for 2026-05-15 reconciliation_mismatch overwrite.

        Sequence observed in production:
          1. Tick 1 (09:30 ET): strategy fires market exit → submits to
             Alpaca, sets `positions.status='CLOSING'` and writes the
             returned order_id to `alpaca_exit_order_id`. Order fills
             within the same tick.
          2. Tick 2 (09:35 ET): recovery loads positions where status
             NOT IN ('CLOSED','ENTRY_FAILED'), sees CLOSING row whose
             ticker is no longer on Alpaca, and writes
             `exit_reason='reconciliation_mismatch'` with NULL
             exit_price/pnl — clobbering the proper exit attribution
             the next-step `_check_order_statuses` was about to write.

        Fix: when status indicates an exit-in-flight AND the row has
        a non-NULL alpaca_*_order_id to poll, defer the close to
        `_check_order_statuses` so it can write the real exit row.
        """
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id,
                alpaca_exit_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "SPY", "NASDAQ", "USD", 1, 700.0,
                (datetime.now(ET) - timedelta(hours=1)).isoformat(),
                "CLOSING", "swing", 1, "overnight_drift",
                "broker-exit-order-id",
            ),
        )
        conn.commit()
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()

        assert "SPY" in result.positions_deferred_exit_inflight, (
            "CLOSING + exit_order_id must defer to _check_order_statuses "
            "instead of writing reconciliation_mismatch"
        )
        assert "SPY" not in result.positions_closed_mismatch

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE ticker='SPY'"
        ).fetchone()
        conn.close()
        assert row[0] == "CLOSING", (
            "deferred row must remain CLOSING so the next tick's "
            "_check_order_statuses can transition it to CLOSED with "
            "real exit_price/pnl"
        )

    @pytest.mark.asyncio
    async def test_db_position_stop_active_with_stop_order_deferred(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """STOP_ACTIVE with a stop_order_id: a broker-side stop almost
        certainly filled when Alpaca no longer shows the position. Defer
        so `_check_order_statuses` writes `exit_reason='stop_loss'` with
        the real exit_price, not reconciliation_mismatch with NULL pnl.
        """
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id,
                alpaca_stop_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "AAPL", "NASDAQ", "USD", 1, 200.0,
                (datetime.now(ET) - timedelta(hours=2)).isoformat(),
                "STOP_ACTIVE", "swing", 1, "mean_reversion",
                "broker-stop-order-id",
            ),
        )
        conn.commit()
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()

        assert "AAPL" in result.positions_deferred_exit_inflight
        assert "AAPL" not in result.positions_closed_mismatch

    @pytest.mark.asyncio
    async def test_db_position_exit_inflight_without_order_id_falls_through(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Defence-in-depth: a row in an exit-in-flight status but with
        NULL order_id can't be reconciled by `_check_order_statuses`
        (no order to poll). Fall through to reconciliation_mismatch
        rather than deferring indefinitely. Pre-PR (a) emergency stops
        would land in this shape; post-PR (a) they shouldn't.
        """
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "MSFT", "NASDAQ", "USD", 1, 400.0,
                (datetime.now(ET) - timedelta(hours=1)).isoformat(),
                "STOP_ACTIVE", "swing", 1, "mean_reversion",
            ),
        )
        conn.commit()
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()

        assert "MSFT" not in result.positions_deferred_exit_inflight, (
            "no order_id means nothing to poll — must not defer"
        )
        assert "MSFT" in result.positions_closed_mismatch

    @pytest.mark.asyncio
    async def test_db_position_open_status_not_deferred(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """POSITION_OPEN WITHOUT a tracked stop still reconciles to
        mismatch when the broker doesn't show the position — there's no
        stop order to poll, so deferring would be indefinite.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "GOOGL")  # no alpaca_stop_order_id
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()
        assert "GOOGL" in result.positions_closed_mismatch
        assert "GOOGL" not in result.positions_deferred_exit_inflight

    @pytest.mark.asyncio
    async def test_db_position_open_with_stop_order_deferred(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """POSITION_OPEN WITH a standalone stop_order_id must defer.
        Standalone-stop sleeves (mean_reversion / overnight_drift /
        opening_range_breakout) never leave POSITION_OPEN, so when their
        protective stop fills between ticks the row must defer to the
        order-status poll for clean stop_loss attribution — not be swept
        to reconciliation_mismatch with NULL pnl (the NVDA/XLF/XLB path
        on 2026-05-29).
        """
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id,
                alpaca_stop_order_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "NVDA", "NASDAQ", "USD", 1, 216.9,
                (datetime.now(ET) - timedelta(hours=2)).isoformat(),
                "POSITION_OPEN", "intraday", 3, "opening_range_breakout",
                "orb-stop-order-id",
            ),
        )
        conn.commit()
        conn.close()

        gw = self._make_gateway(positions=[])
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier,
            now=datetime.now(ET) + timedelta(hours=1),
        )
        result = await recovery.recover()

        assert "NVDA" in result.positions_deferred_exit_inflight
        assert "NVDA" not in result.positions_closed_mismatch

    @pytest.mark.asyncio
    async def test_quantity_mismatch_updated(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Alpaca qty=90, DB qty=100 → DB updated to 90."""
        conn = sqlite3.connect(tmp_db_path)
        pos_id = _insert_db_position(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=90)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()
        assert "PLTR" in result.quantities_updated

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT quantity FROM positions WHERE id=?", (pos_id,)
        ).fetchone()
        conn.close()
        assert row[0] == 90

    @pytest.mark.asyncio
    async def test_stop_order_placed_if_missing(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Open position with no stop order → stop placed."""
        pos = _alpaca_position("PLTR", qty=100, avg_entry_price=10.0)
        gw = self._make_gateway(positions=[pos], orders=[])

        # Pin the clock to mid-session (11:00 ET) so the new
        # close-window gate doesn't skip placement.
        mid_session = datetime(2026, 5, 11, 11, 0, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=mid_session)
        result = await recovery.recover()

        assert "PLTR" in result.stops_placed
        gw.client.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_emergency_stop_skipped_when_avg_cost_zero(
        self, tmp_db_path: str, mock_notifier,
    ) -> None:
        """avg_cost <= 0 means we can't compute a valid stop price.
        Recovery should log an error and skip rather than place a
        bogus stop at price 0."""
        pos = _alpaca_position("PLTR", qty=100, avg_entry_price=0.0)
        gw = self._make_gateway(positions=[pos], orders=[])

        mid_session = datetime(2026, 5, 11, 11, 0, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=mid_session)
        await recovery.recover()

        # Stop should NOT be submitted when avg_cost is 0
        gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_emergency_stop_persists_order_id_to_db(
        self, tmp_db_path: str, mock_notifier,
    ) -> None:
        """Regression for 2026-05-15 untracked-emergency-stop bug.

        ``_place_emergency_stop`` submitted a stop to Alpaca and dropped
        the returned order_id on the floor, leaving
        ``positions.alpaca_stop_order_id`` NULL forever. Every subsequent
        tick saw the position as naked, accumulated broker-side stops,
        and when one finally filled, recovery wrote
        ``exit_reason='reconciliation_mismatch'`` with NULL exit_price/
        pnl — wiping the realized-P&L attribution from
        ``daily_summaries``.

        Fix: capture ``order.id`` from ``submit_order`` and write it
        back via ``_persist_emergency_stop_id``.
        """
        conn = sqlite3.connect(tmp_db_path)
        position_id = _insert_db_position(conn, "PLTR", qty=100, entry_price=10.0)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100, avg_entry_price=10.0)
        gw = self._make_gateway(positions=[pos], orders=[])
        # Mock the broker's submit_order to return an order with an id —
        # the real Alpaca SDK does this; the prior mock setup was lenient
        # enough that the dropped return value went unnoticed.
        submitted_order = MagicMock()
        submitted_order.id = "broker-stop-id-xyz"
        gw.client.submit_order = MagicMock(return_value=submitted_order)

        mid_session = datetime(2026, 5, 11, 11, 0, tzinfo=ET)
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier, now=mid_session,
        )
        result = await recovery.recover()
        assert "PLTR" in result.stops_placed

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT alpaca_stop_order_id, stop_price FROM positions "
            "WHERE id = ?",
            (position_id,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "broker-stop-id-xyz", (
            "emergency stop's order_id must be persisted so "
            "_check_order_statuses can poll for fills cleanly"
        )
        # 2% default stop_loss_pct applied to entry $10.00.
        assert row[1] == pytest.approx(9.80, abs=1e-6)

    @pytest.mark.asyncio
    async def test_emergency_stop_does_not_clobber_existing_stop_price(
        self, tmp_db_path: str, mock_notifier,
    ) -> None:
        """When a strategy already set a custom stop_price on the row,
        the emergency-stop writeback must preserve it (COALESCE).

        Recovery's stop price comes from a generic default-pct config and
        is intentionally a fallback. If a strategy chose a tighter or
        looser stop, that intent must survive the writeback — otherwise
        every emergency-stop placement would silently retune the row to
        the default.
        """
        conn = sqlite3.connect(tmp_db_path)
        cur = conn.execute(
            """INSERT INTO positions
               (ticker, exchange, currency, quantity, entry_price,
                entry_time, status, stop_price, hold_type, phase,
                strategy_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "PLTR", "NASDAQ", "USD", 100, 10.0,
                datetime.now(ET).isoformat(),
                "POSITION_OPEN", 9.50, "swing", 1, "mr",
            ),
        )
        position_id = cur.lastrowid
        conn.commit()
        conn.close()

        pos = _alpaca_position("PLTR", qty=100, avg_entry_price=10.0)
        gw = self._make_gateway(positions=[pos], orders=[])
        submitted_order = MagicMock()
        submitted_order.id = "broker-stop-id-xyz"
        gw.client.submit_order = MagicMock(return_value=submitted_order)

        mid_session = datetime(2026, 5, 11, 11, 0, tzinfo=ET)
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier, now=mid_session,
        )
        await recovery.recover()

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT alpaca_stop_order_id, stop_price FROM positions "
            "WHERE id = ?",
            (position_id,),
        ).fetchone()
        conn.close()
        assert row[0] == "broker-stop-id-xyz"
        assert row[1] == pytest.approx(9.50, abs=1e-6), (
            "strategy-set stop_price 9.50 must survive; recovery's "
            "default 9.80 must not overwrite"
        )

    @pytest.mark.asyncio
    async def test_emergency_stop_persistence_handles_missing_db_row(
        self, tmp_db_path: str, mock_notifier,
    ) -> None:
        """If the broker has a position with no DB row, recovery first
        creates a row via ``_create_db_position`` and then places a
        stop. The persistence writeback must succeed against that
        freshly-created row.
        """
        pos = _alpaca_position("PLTR", qty=100, avg_entry_price=10.0)
        gw = self._make_gateway(positions=[pos], orders=[])
        submitted_order = MagicMock()
        submitted_order.id = "broker-stop-id-xyz"
        gw.client.submit_order = MagicMock(return_value=submitted_order)

        mid_session = datetime(2026, 5, 11, 11, 0, tzinfo=ET)
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier, now=mid_session,
        )
        result = await recovery.recover()
        assert "PLTR" in result.positions_created
        assert "PLTR" in result.stops_placed

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT alpaca_stop_order_id FROM positions WHERE ticker='PLTR'"
        ).fetchone()
        conn.close()
        assert row is not None and row[0] == "broker-stop-id-xyz"

    @pytest.mark.asyncio
    async def test_stop_verify_skipped_at_close_of_day_tick(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Regression for 2026-05-08 phantom-stop bug.

        At the 16:00 ET (= 20:00 UTC) close-of-day tick, the existing
        DAY stops have JUST expired at the bell. _verify_stop_orders
        would see them missing and submit fresh stops — which Alpaca
        defers to the next session's pre-market open, where they hold
        the qty and block the morning exit.

        The gate must skip _place_emergency_stop in the close window.
        Next morning's first tick will re-attach a fresh stop in a
        window where Alpaca submits it immediately.
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        # 16:00 ET exactly — the cron tick that caused the regression.
        close_tick = datetime(2026, 5, 8, 16, 0, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=close_tick)
        result = await recovery.recover()

        gw.client.submit_order.assert_not_called(), (
            "no stop must be submitted at the close-of-day tick — "
            "Alpaca defers post-close DAY orders to next session, "
            "creating phantom stops that block morning exits"
        )
        assert "XLF" not in result.stops_placed

    @pytest.mark.asyncio
    async def test_stop_verify_skipped_just_before_close(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """15:55 ET is the cutoff — at or after, defer to next session."""
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        just_before_close = datetime(2026, 5, 8, 15, 55, tzinfo=ET)
        recovery = _make_recovery(
            gw, tmp_db_path, mock_notifier, now=just_before_close,
        )
        await recovery.recover()
        gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_verify_skipped_pre_market(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Pre-market (e.g., 08:00 ET) is also outside the safe window.
        DAY stops placed pre-market would still be queued before the
        regular session starts.
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])
        pre_market = datetime(2026, 5, 8, 8, 0, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=pre_market)
        await recovery.recover()
        gw.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_verify_active_at_market_open(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """09:30 ET is the open — the first valid window for stop placement.
        This is the morning tick that re-attaches the stop the close-of-day
        skipper deferred.
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])
        market_open = datetime(2026, 5, 8, 9, 30, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=market_open)
        result = await recovery.recover()

        assert "XLF" in result.stops_placed
        gw.client.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_verify_uses_alpaca_clock_when_closed(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """When Alpaca's clock reports is_open=False, defer regardless of
        wall-clock time. Closes the early-close-day gap that the fixed
        15:55 ET cutoff couldn't see (Thanksgiving Friday closes 13:00 ET,
        the bot's 14:00 tick would otherwise place a stop that Alpaca
        queues until the next session).
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        # Wall clock says 14:00 ET — inside the fixed time gate's safe
        # window. But Alpaca's clock says the market is closed (e.g.,
        # early-close day post-13:00).
        clock = MagicMock()
        clock.is_open = False
        clock.next_close = None
        gw.client.get_clock = MagicMock(return_value=clock)

        wall_clock = datetime(2026, 11, 28, 14, 0, tzinfo=ET)  # Thxgv Fri
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=wall_clock)
        await recovery.recover()

        gw.client.submit_order.assert_not_called(), (
            "AlpacaClock.is_open=False must override the fixed time gate "
            "— early-close days would otherwise reproduce the phantom-stop bug"
        )

    @pytest.mark.asyncio
    async def test_stop_verify_uses_alpaca_clock_near_close(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """When market is open but ``next_close`` is within the 5-min
        buffer, defer. Catches the boundary case where Alpaca would still
        queue a DAY order rather than submit it before close.
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        wall_clock = datetime(2026, 5, 8, 15, 57, tzinfo=ET)
        # Clock says open, but next_close is 3 min away — below the 5-min
        # safety buffer.
        clock = MagicMock()
        clock.is_open = True
        clock.next_close = datetime(2026, 5, 8, 16, 0, tzinfo=ET)
        gw.client.get_clock = MagicMock(return_value=clock)

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=wall_clock)
        await recovery.recover()

        gw.client.submit_order.assert_not_called(), (
            "within 5 min of close — DAY stop would be queued for next "
            "session, must defer"
        )

    @pytest.mark.asyncio
    async def test_stop_verify_active_when_clock_open_and_buffer_safe(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Clock says open + plenty of time before close → place the stop."""
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        wall_clock = datetime(2026, 5, 8, 11, 0, tzinfo=ET)
        clock = MagicMock()
        clock.is_open = True
        clock.next_close = datetime(2026, 5, 8, 16, 0, tzinfo=ET)
        gw.client.get_clock = MagicMock(return_value=clock)

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=wall_clock)
        result = await recovery.recover()

        assert "XLF" in result.stops_placed
        gw.client.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_verify_falls_back_when_clock_unavailable(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """If Alpaca's clock endpoint flakes, fall back to the fixed time
        gate so the safety net still works (and we don't accidentally
        gate-everything when the clock call errors).
        """
        pos = _alpaca_position("XLF", qty=1.989, avg_entry_price=51.28)
        gw = self._make_gateway(positions=[pos], orders=[])

        # Clock call raises.
        gw.client.get_clock = MagicMock(side_effect=RuntimeError("clock 503"))

        # Wall clock at 11:00 ET — inside the fallback safe window.
        wall_clock = datetime(2026, 5, 8, 11, 0, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=wall_clock)
        result = await recovery.recover()

        assert "XLF" in result.stops_placed
        gw.client.submit_order.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_discrepancy_clean_state(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Alpaca and DB agree — no discrepancies."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        stop_order = _alpaca_order("PLTR", order_type="stop")
        gw = self._make_gateway(positions=[pos], orders=[stop_order])

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()
        assert not result.has_discrepancies

    @pytest.mark.asyncio
    async def test_stale_entry_order_cancelled(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """A 'limit' order older than the threshold is cancelled."""
        old_ts = datetime.now(ET) - timedelta(minutes=10)
        stale = _alpaca_order("PLTR", order_type="limit", submitted_at=old_ts, order_id="abc")
        gw = self._make_gateway(positions=[], orders=[stale])

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()

        assert "PLTR" in result.stale_orders_cancelled
        gw.client.cancel_order_by_id.assert_called_once_with("abc")

    @pytest.mark.asyncio
    async def test_fresh_entry_order_not_cancelled(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """A recently submitted entry order is left alone."""
        fresh_ts = datetime.now(ET) - timedelta(seconds=30)
        fresh = _alpaca_order("PLTR", order_type="limit", submitted_at=fresh_ts)
        gw = self._make_gateway(positions=[], orders=[fresh])

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()

        assert not result.stale_orders_cancelled
        gw.client.cancel_order_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_orders_never_cancelled_as_stale(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Stop and trailing-stop orders are GTC by design — never stale."""
        old_ts = datetime.now(ET) - timedelta(hours=4)
        old_stop = _alpaca_order("PLTR", order_type="stop", submitted_at=old_ts)
        # Need a position so the existing stop-verification step doesn't try
        # to place a fresh stop and call submit_order.
        pos = _alpaca_position("PLTR", qty=100)
        gw = self._make_gateway(positions=[pos], orders=[old_stop])

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()

        assert not result.stale_orders_cancelled
        gw.client.cancel_order_by_id.assert_not_called()

    @pytest.mark.asyncio
    async def test_eod_flatten_closes_intraday_position(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Past 15:55 ET: intraday-tagged position is flattened."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position_intraday(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        # Stop already exists so the stop-verifier doesn't call submit_order.
        stop = _alpaca_order("PLTR", order_type="stop")
        gw = self._make_gateway(positions=[pos], orders=[stop])

        eod_now = datetime(2026, 4, 28, 15, 56, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=eod_now)
        result = await recovery.recover()

        assert "PLTR" in result.eod_flatten_orders
        # Stop already exists, so only the flatten market-order is submitted.
        assert gw.client.submit_order.call_count == 1

    @pytest.mark.asyncio
    async def test_eod_flatten_skipped_for_swing_positions(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Swing-tagged positions are not flattened at EOD."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position(conn, "PLTR", qty=100)  # default hold_type='swing'
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        stop = _alpaca_order("PLTR", order_type="stop")
        gw = self._make_gateway(positions=[pos], orders=[stop])

        eod_now = datetime(2026, 4, 28, 15, 58, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=eod_now)
        result = await recovery.recover()

        assert not result.eod_flatten_orders

    @pytest.mark.asyncio
    async def test_eod_flatten_skipped_before_cutoff(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Before 15:55 ET, even intraday positions are kept open."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position_intraday(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        stop = _alpaca_order("PLTR", order_type="stop")
        gw = self._make_gateway(positions=[pos], orders=[stop])

        midday = datetime(2026, 4, 28, 11, 30, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=midday)
        result = await recovery.recover()

        assert not result.eod_flatten_orders

    @pytest.mark.asyncio
    async def test_eod_flatten_idempotent_across_ticks(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Regression for review CRITICAL-4: a 5-min cron after 15:55
        must not submit a fresh market SELL on every tick. After the
        first successful submission the DB row transitions to CLOSING,
        so the second tick's _intraday_tickers_in_db excludes it.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position_intraday(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        stop = _alpaca_order("PLTR", order_type="stop")
        gw = self._make_gateway(positions=[pos], orders=[stop])

        eod_now = datetime(2026, 4, 28, 15, 56, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=eod_now)

        # Tick 1 — submits flatten, marks DB CLOSING.
        result1 = await recovery.recover()
        assert "PLTR" in result1.eod_flatten_orders
        first_call_count = gw.client.submit_order.call_count
        assert first_call_count == 1

        # Verify the DB row transitioned.
        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE ticker = 'PLTR'"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "CLOSING"

        # Tick 2 (next 5-min cron, position not yet filled) — must NOT
        # submit a duplicate. Reuse the same recovery instance + clock.
        result2 = await recovery.recover()
        assert "PLTR" not in result2.eod_flatten_orders
        assert gw.client.submit_order.call_count == first_call_count, (
            "second tick must NOT submit a duplicate flatten — "
            "regression: pre-fix would re-fire every tick"
        )

    @pytest.mark.asyncio
    async def test_eod_flatten_skips_when_pending_sell_on_alpaca(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """Belt-and-braces idempotency: if an Alpaca-side SELL is
        already pending (e.g., DB write failed after a prior submit),
        skip rather than double up.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_db_position_intraday(conn, "PLTR", qty=100)
        conn.close()

        pos = _alpaca_position("PLTR", qty=100)
        # Pre-existing stop so the stop-verifier doesn't submit one.
        stop = _alpaca_order("PLTR", order_type="stop")
        # An open SELL order already exists on Alpaca for this ticker.
        pending_sell = MagicMock()
        pending_sell.symbol = "PLTR"
        pending_sell.side = MagicMock()
        pending_sell.side.value = "sell"
        pending_sell.status = MagicMock()
        pending_sell.status.value = "new"
        pending_sell.type = MagicMock()
        pending_sell.type.value = "market"
        pending_sell.id = "open-sell-1"
        pending_sell.submitted_at = datetime.now(ET)
        pending_sell.created_at = pending_sell.submitted_at

        gw = self._make_gateway(positions=[pos], orders=[stop, pending_sell])

        eod_now = datetime(2026, 4, 28, 15, 56, tzinfo=ET)
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier, now=eod_now)
        result = await recovery.recover()

        assert "PLTR" not in result.eod_flatten_orders
        # No new submit_order — only pre-existing orders observed.
        assert gw.client.submit_order.call_count == 0


# ---------------------------------------------------------------------------
# Orphan-attribution recovery (2026-05-11 XLK regression)
# ---------------------------------------------------------------------------


def _insert_entry_failed(
    conn: sqlite3.Connection,
    ticker: str,
    qty: float,
    entry_price: float,
    strategy_id: str = "overnight_drift",
    minutes_ago: int = 5,
) -> int:
    entry_time = (datetime.now(ET) - timedelta(minutes=minutes_ago)).isoformat()
    cur = conn.execute(
        """INSERT INTO positions
           (ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id, alpaca_order_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ticker, "US", "USD", qty, entry_price, entry_time,
            "ENTRY_FAILED", "swing", 3, strategy_id, "entry-order-abc",
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _insert_unknown_orphan(
    conn: sqlite3.Connection,
    ticker: str,
    qty: float,
    entry_price: float,
    minutes_ago: int = 0,
) -> int:
    entry_time = (datetime.now(ET) - timedelta(minutes=minutes_ago)).isoformat()
    cur = conn.execute(
        """INSERT INTO positions
           (ticker, exchange, currency, quantity, entry_price, entry_time,
            status, hold_type, phase, strategy_id)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            ticker, "AssetExchange.ARCA", "USD", qty, entry_price, entry_time,
            "POSITION_OPEN", "swing", 1, "unknown",
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


class TestOrphanAttributionRecovery:
    """Regression suite for the 2026-05-11 XLK orphan-attribution bug.

    Symptom: 169 ms Alpaca fill on a fractional overnight_drift entry,
    5-min local entry-pending sweep flipped the row to ENTRY_FAILED
    before the fill was observed, then the next reconcile tick saw the
    unattached Alpaca position and created a duplicate row with
    strategy_id='unknown'. Overnight_drift's exit logic then refused to
    fire on the orphan, leaving the position long indefinitely.
    """

    def _make_gateway(self, positions: list | None = None) -> MagicMock:
        gw = MagicMock()
        gw.account_id = "TEST_ACCOUNT"
        gw.client = MagicMock()
        gw.get_positions = AsyncMock(return_value=positions or [])
        gw.get_open_orders = AsyncMock(return_value=[])
        gw.get_account_summary = AsyncMock(
            return_value={
                "NetLiquidation": "100000.0",
                "SettledCash": "99000.0",
                "BuyingPower": "99000.0",
            }
        )
        return gw

    @pytest.mark.asyncio
    async def test_create_db_position_reattributes_to_matching_entry_failed(
        self, tmp_db_path: str, mock_notifier
    ):
        """When Alpaca exposes a position not in the open DB rows but
        matching a recent ENTRY_FAILED row, _create_db_position must
        promote the ENTRY_FAILED row instead of inserting a new orphan
        with strategy_id='unknown'.
        """
        conn = sqlite3.connect(tmp_db_path)
        failed_id = _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=5,
        )
        conn.close()

        pos = _alpaca_position("XLK", qty=0.3927, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        await recovery.recover()

        conn = sqlite3.connect(tmp_db_path)
        try:
            rows = conn.execute(
                "SELECT id, status, strategy_id FROM positions "
                "WHERE ticker = 'XLK' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        # Only ONE XLK row — the ENTRY_FAILED row got promoted, no
        # new orphan was created.
        assert len(rows) == 1, (
            f"expected single reattributed row, got {len(rows)} rows: {rows}"
        )
        assert rows[0][0] == failed_id
        assert rows[0][1] == "POSITION_OPEN"
        assert rows[0][2] == "overnight_drift", (
            "strategy attribution must be preserved from the original "
            "ENTRY_FAILED row, not collapsed to 'unknown'"
        )

    @pytest.mark.asyncio
    async def test_create_db_position_no_match_still_creates_unknown_orphan(
        self, tmp_db_path: str, mock_notifier
    ):
        """Negative case: when no ENTRY_FAILED row matches, fall back to
        the legacy strategy_id='unknown' insert so the position is at
        least tracked (some other recovery path may attribute it later).
        """
        pos = _alpaca_position("XLB", qty=10.0, avg_entry_price=50.0)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        await recovery.recover()

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status, strategy_id FROM positions WHERE ticker = 'XLB'"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "POSITION_OPEN"
        assert row[1] == "unknown"

    @pytest.mark.asyncio
    async def test_create_db_position_quantity_mismatch_does_not_merge(
        self, tmp_db_path: str, mock_notifier
    ):
        """Quantity mismatch (e.g. partial fill) must NOT merge — that
        would attribute the wrong size to the original strategy. Falls
        through to the unknown-orphan path.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=5,
        )
        conn.close()
        # Half-share off — clearly a different fill.
        pos = _alpaca_position("XLK", qty=0.5, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        await recovery.recover()

        conn = sqlite3.connect(tmp_db_path)
        try:
            statuses = {
                row[0]: row[1] for row in conn.execute(
                    "SELECT status, strategy_id FROM positions "
                    "WHERE ticker = 'XLK'"
                ).fetchall()
            }
        finally:
            conn.close()
        # ENTRY_FAILED still ENTRY_FAILED, plus a new POSITION_OPEN orphan.
        assert statuses.get("ENTRY_FAILED") == "overnight_drift"
        assert statuses.get("POSITION_OPEN") == "unknown"

    @pytest.mark.asyncio
    async def test_create_db_position_stale_entry_failed_does_not_merge(
        self, tmp_db_path: str, mock_notifier
    ):
        """ENTRY_FAILED rows older than the reattribution window must NOT
        merge — they were genuinely failed entries, not late fills.
        """
        conn = sqlite3.connect(tmp_db_path)
        _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=120,  # 2h old
        )
        conn.close()
        pos = _alpaca_position("XLK", qty=0.3927, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        await recovery.recover()

        conn = sqlite3.connect(tmp_db_path)
        try:
            rows = conn.execute(
                "SELECT status, strategy_id FROM positions "
                "WHERE ticker = 'XLK'"
            ).fetchall()
        finally:
            conn.close()
        # Two rows: stale ENTRY_FAILED unchanged + new unknown orphan
        assert len(rows) == 2
        statuses = {r[0] for r in rows}
        assert "ENTRY_FAILED" in statuses
        assert "POSITION_OPEN" in statuses

    @pytest.mark.asyncio
    async def test_heal_unknown_orphans_merges_existing_pair(
        self, tmp_db_path: str, mock_notifier
    ):
        """Live-incident-specific case: position #97 already exists in
        the DB as a strategy_id='unknown' POSITION_OPEN orphan, and
        position #96 is the matching ENTRY_FAILED row from the prior
        tick. _heal_unknown_orphans must merge them.
        """
        conn = sqlite3.connect(tmp_db_path)
        failed_id = _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=15,
        )
        orphan_id = _insert_unknown_orphan(
            conn, "XLK", qty=0.3927, entry_price=177.438, minutes_ago=10,
        )
        conn.close()

        # Alpaca still reports the position so the reconciler treats
        # the now-promoted ENTRY_FAILED row as the canonical match.
        pos = _alpaca_position("XLK", qty=0.3927, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()

        assert any("XLK" in s for s in result.orphans_healed), (
            f"expected orphan heal in result, got {result.orphans_healed}"
        )

        conn = sqlite3.connect(tmp_db_path)
        try:
            failed_status = conn.execute(
                "SELECT status, strategy_id FROM positions WHERE id = ?",
                (failed_id,),
            ).fetchone()
            orphan_status = conn.execute(
                "SELECT status FROM positions WHERE id = ?", (orphan_id,),
            ).fetchone()
        finally:
            conn.close()
        # ENTRY_FAILED promoted to POSITION_OPEN with original strategy
        assert failed_status[0] == "POSITION_OPEN"
        assert failed_status[1] == "overnight_drift"
        # Orphan row closed out so it doesn't double-count
        assert orphan_status[0] == "CLOSED"

    @pytest.mark.asyncio
    async def test_heal_unknown_orphans_idempotent(
        self, tmp_db_path: str, mock_notifier
    ):
        """Running recovery twice must not double-merge or thrash state."""
        conn = sqlite3.connect(tmp_db_path)
        _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=15,
        )
        _insert_unknown_orphan(
            conn, "XLK", qty=0.3927, entry_price=177.438, minutes_ago=10,
        )
        conn.close()

        pos = _alpaca_position("XLK", qty=0.3927, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        first = await recovery.recover()
        second = await recovery.recover()

        assert first.orphans_healed
        # Second pass: no new healing work to do — the original orphan
        # is now CLOSED and there's no remaining unknown POSITION_OPEN.
        assert second.orphans_healed == []

    @pytest.mark.asyncio
    async def test_heal_unknown_orphans_atomic_under_double_claim(
        self, tmp_db_path: str, mock_notifier
    ):
        """Regression for SELECT-then-UPDATE race in _heal_unknown_orphans.

        The earlier implementation used a SELECT to find an orphan/
        ENTRY_FAILED pair then a separate UPDATE inside `with conn:`.
        Two concurrent ticks could both observe the same pair and both
        attempt the merge, inflating the heal count and (worse) producing
        a second POSITION_OPEN row from the same broker holding if the
        ENTRY_FAILED row was already promoted.

        With the atomic single-UPDATE-with-rowcount-check, the second
        attempt must report no healing and the DB must reflect exactly
        ONE POSITION_OPEN row for the ticker.
        """
        conn = sqlite3.connect(tmp_db_path)
        failed_id = _insert_entry_failed(
            conn, "XLK", qty=0.3927, entry_price=177.438,
            strategy_id="overnight_drift", minutes_ago=15,
        )
        orphan_id = _insert_unknown_orphan(
            conn, "XLK", qty=0.3927, entry_price=177.438, minutes_ago=10,
        )
        conn.close()

        pos = _alpaca_position("XLK", qty=0.3927, avg_entry_price=177.438)
        gw = self._make_gateway(positions=[pos])

        # Simulate two ticks racing against the same state by building
        # two recovery instances against the same DB path and calling
        # _heal_unknown_orphans directly twice in sequence. The atomic
        # UPDATE in the second call must observe status != ENTRY_FAILED
        # and report no heal.
        from trading_bot.gateway.recovery import RecoveryResult
        recovery_a = _make_recovery(gw, tmp_db_path, mock_notifier)
        recovery_b = _make_recovery(gw, tmp_db_path, mock_notifier)

        result_a = RecoveryResult()
        result_b = RecoveryResult()
        recovery_a._heal_unknown_orphans(result_a)
        recovery_b._heal_unknown_orphans(result_b)

        # First call did the work; second saw a clean DB.
        assert len(result_a.orphans_healed) == 1
        assert result_b.orphans_healed == []

        # End-state must have exactly ONE POSITION_OPEN row for XLK —
        # the promoted ENTRY_FAILED row — and the original orphan must
        # be CLOSED. No duplicate POSITION_OPEN.
        conn = sqlite3.connect(tmp_db_path)
        try:
            rows = conn.execute(
                "SELECT id, status, strategy_id FROM positions "
                "WHERE ticker = 'XLK' ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        open_rows = [r for r in rows if r[1] == "POSITION_OPEN"]
        assert len(open_rows) == 1, (
            f"expected exactly 1 POSITION_OPEN after double-heal, "
            f"got {len(open_rows)} rows: {rows}"
        )
        assert open_rows[0][0] == failed_id
        assert open_rows[0][2] == "overnight_drift"
        # Original orphan must be CLOSED.
        closed_orphan = [
            r for r in rows if r[0] == orphan_id and r[1] == "CLOSED"
        ]
        assert closed_orphan, (
            f"orphan #{orphan_id} must be CLOSED after heal, got {rows}"
        )

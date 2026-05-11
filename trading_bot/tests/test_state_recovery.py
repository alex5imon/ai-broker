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

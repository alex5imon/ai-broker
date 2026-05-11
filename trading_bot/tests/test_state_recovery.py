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

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
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

        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        await recovery.recover()

        # Stop should NOT be submitted when avg_cost is 0
        gw.client.submit_order.assert_not_called()

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

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
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()
        assert "PLTR" in result.positions_closed_mismatch

        conn = sqlite3.connect(tmp_db_path)
        row = conn.execute(
            "SELECT status FROM positions WHERE ticker='PLTR'"
        ).fetchone()
        conn.close()
        assert row[0] == "CLOSED"

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
    async def test_settlements_updated_on_recovery(
        self, tmp_db_path: str, mock_notifier
    ) -> None:
        """update_settlements is called during recovery."""
        from datetime import date

        conn = sqlite3.connect(tmp_db_path)
        today = date.today().isoformat()
        conn.execute(
            """INSERT INTO settlements (ticker, amount, currency, amount_gbp,
               sell_date, settle_date, settled) VALUES (?,?,?,?,?,?,0)""",
            ("PLTR", 100.0, "USD", 80.0, today, today),
        )
        conn.commit()
        conn.close()

        gw = self._make_gateway()
        recovery = _make_recovery(gw, tmp_db_path, mock_notifier)
        result = await recovery.recover()
        assert result.settlements_updated >= 1

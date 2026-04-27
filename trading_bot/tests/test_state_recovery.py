"""Tests for StateRecovery (startup reconciliation)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_recovery(gateway, db_path: str, notifier) -> Any:
    from trading_bot.gateway.recovery import StateRecovery
    return StateRecovery(
        gateway=gateway,
        db_path=db_path,
        notifier=notifier,
        config={
            "exit_intraday": {"stop_loss_pct": 0.02},
        },
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
) -> MagicMock:
    order = MagicMock()
    order.symbol = ticker
    order.type = MagicMock()
    order.type.value = order_type
    return order


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

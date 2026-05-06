"""Tests for the orphan-position repair script.

Covers the two repair classes:
  - ``ENTRY_FAILED`` rows whose Alpaca order actually filled.
  - ``strategy_id='unknown'`` POSITION_OPEN rows that duplicate a real
    strategy entry on the same ticker.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.self_improve.repair_orphans import repair


def _alpaca_order(*, status: str = "filled", filled_qty: float = 0.0,
                  filled_avg_price: float = 0.0):
    o = MagicMock()
    o.status = MagicMock()
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    return o


def _alpaca_open_stop(*, order_id: str = "stop-1", symbol: str = "SPY",
                     qty: float = 1.0, side: str = "sell",
                     order_type: str = "stop"):
    o = MagicMock()
    o.id = order_id
    o.symbol = symbol
    o.qty = qty
    o.side = MagicMock(); o.side.value = side
    o.order_type = MagicMock(); o.order_type.value = order_type
    return o


def _seed_position(
    db_path: str, *, ticker: str, status: str, strategy_id: str,
    alpaca_order_id: str | None = None, qty: float = 1.0,
    entry_price: float = 100.0,
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, "
            "quantity, entry_price, entry_time, status, hold_type, "
            "phase, strategy_id, alpaca_order_id) VALUES "
            "(?, 'US', 'USD', ?, ?, '2026-05-05T15:45:00', ?, "
            "'overnight', 1, ?, ?)",
            (ticker, qty, entry_price, status, strategy_id, alpaca_order_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_repair_flips_entry_failed_to_active_when_order_filled_with_stop(
    tmp_db_path: str,
):
    pos_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1",
        qty=0.204, entry_price=720.0,  # mid-submit price; will be overwritten
    )
    client = MagicMock()
    client.get_order_by_id = MagicMock(
        return_value=_alpaca_order(
            status="filled", filled_qty=0.204, filled_avg_price=724.72,
        )
    )
    client.get_orders = MagicMock(
        return_value=[_alpaca_open_stop(order_id="stop-recovered",
                                       symbol="SPY", qty=0.204)]
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    assert report.entry_failed_scanned == 1
    assert report.entry_failed_repaired == 1

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT status, entry_price, quantity, alpaca_stop_order_id "
            "FROM positions WHERE id = ?", (pos_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == PositionStatus.STOP_AND_TARGET_ACTIVE.value
    # entry_price overwritten with the actual broker fill avg
    assert abs(row[1] - 724.72) < 1e-6
    assert abs(row[2] - 0.204) < 1e-6
    assert row[3] == "stop-recovered"


@pytest.mark.asyncio
async def test_repair_flips_to_position_open_when_no_matching_stop_exists(
    tmp_db_path: str,
):
    """Entry filled but no stop on broker — flip to POSITION_OPEN so the
    next reconciler tick attaches an emergency stop. STOP_AND_TARGET_ACTIVE
    would lie about state."""
    pos_id = _seed_position(
        tmp_db_path, ticker="QQQ",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-2", qty=0.1482, entry_price=680.0,
    )
    client = MagicMock()
    client.get_order_by_id = MagicMock(
        return_value=_alpaca_order(
            status="filled", filled_qty=0.1482, filled_avg_price=682.07,
        )
    )
    client.get_orders = MagicMock(return_value=[])

    conn = sqlite3.connect(tmp_db_path)
    try:
        await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status, alpaca_stop_order_id "
            "FROM positions WHERE id = ?", (pos_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == PositionStatus.POSITION_OPEN.value
    assert row[1] is None or row[1] == ""


@pytest.mark.asyncio
async def test_repair_leaves_genuine_failures_alone(tmp_db_path: str):
    """ENTRY_FAILED with order status canceled/expired is a real failure;
    the row must stay ENTRY_FAILED."""
    pos_id = _seed_position(
        tmp_db_path, ticker="XLP",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="mean_reversion",
        alpaca_order_id="entry-canceled",
    )
    client = MagicMock()
    client.get_order_by_id = MagicMock(
        return_value=_alpaca_order(status="canceled")
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (pos_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.entry_failed_repaired == 0, (
        "A genuinely-canceled order must not be flipped — that would "
        "create a phantom POSITION_OPEN with no broker counterpart."
    )
    assert row[0] == PositionStatus.ENTRY_FAILED.value


@pytest.mark.asyncio
async def test_repair_closes_unknown_duplicate_when_real_sibling_exists(
    tmp_db_path: str,
):
    real_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.STOP_AND_TARGET_ACTIVE.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-real",
    )
    unk_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="unknown",
    )
    client = MagicMock()

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        rows = conn.execute(
            "SELECT id, status, strategy_id FROM positions "
            "WHERE ticker = 'SPY' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    assert report.unknown_duplicates_closed == 1
    rows_by_id = {r[0]: r for r in rows}
    assert rows_by_id[real_id][1] == PositionStatus.STOP_AND_TARGET_ACTIVE.value
    assert rows_by_id[unk_id][1] == "CLOSED", (
        "the duplicate must be closed once a real-strategy sibling exists, "
        "otherwise the strategy guard sees neither (its query filters by "
        "strategy_id) and tomorrow's entry double-submits."
    )


@pytest.mark.asyncio
async def test_repair_leaves_unknown_alone_when_no_real_sibling(tmp_db_path: str):
    """An unknown-strategy POSITION_OPEN with no sibling represents a
    broker-side-only position the bot didn't open. Don't close it
    blindly — that would lose audit information about the orphan."""
    unk_id = _seed_position(
        tmp_db_path, ticker="MSFT",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="unknown",
    )
    client = MagicMock()

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (unk_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.unknown_duplicates_closed == 0
    assert row[0] == PositionStatus.POSITION_OPEN.value


@pytest.mark.asyncio
async def test_repair_dry_run_makes_no_writes(tmp_db_path: str):
    pos_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1", qty=0.5, entry_price=700.0,
    )
    client = MagicMock()
    client.get_order_by_id = MagicMock(
        return_value=_alpaca_order(
            status="filled", filled_qty=0.5, filled_avg_price=720.0,
        )
    )
    client.get_orders = MagicMock(return_value=[])

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=True)
        row = conn.execute(
            "SELECT status, entry_price FROM positions WHERE id = ?",
            (pos_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.entry_failed_repaired == 1, (
        "dry-run still counts what it would change"
    )
    assert row[0] == PositionStatus.ENTRY_FAILED.value, (
        "dry-run must not modify the row"
    )
    assert abs(row[1] - 700.0) < 1e-6, "entry_price untouched in dry-run"


@pytest.mark.asyncio
async def test_repair_is_idempotent(tmp_db_path: str):
    """Running on a clean DB finds nothing to do; running twice in a row
    on a fixable DB makes the same DB writes only once."""
    _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1", qty=0.2, entry_price=700.0,
    )
    client = MagicMock()
    client.get_order_by_id = MagicMock(
        return_value=_alpaca_order(
            status="filled", filled_qty=0.2, filled_avg_price=720.0,
        )
    )
    client.get_orders = MagicMock(return_value=[])

    conn = sqlite3.connect(tmp_db_path)
    try:
        first = await repair(conn, client, dry_run=False)
        second = await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    assert first.entry_failed_repaired == 1
    assert second.entry_failed_repaired == 0, (
        "after first repair the row is no longer ENTRY_FAILED, so the "
        "second pass must find no work to do."
    )

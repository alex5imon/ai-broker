"""Tests for the orphan-position repair script.

Three repair classes:
  - ``ENTRY_FAILED`` rows whose Alpaca order actually filled AND ticker
    is currently held → flip to live.
  - ``ENTRY_FAILED`` rows whose order filled but ticker is NOT held →
    mark CLOSED (round-tripped before repair). Catches the bug from
    the original repair script that flipped these to POSITION_OPEN as
    ghost rows.
  - ``strategy_id='unknown'`` POSITION_OPEN rows that duplicate a real
    strategy entry on the same ticker.
  - Phantom live rows: any non-terminal row whose ticker isn't held at
    Alpaca, or older duplicates of a held ticker.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable
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
    o.side = MagicMock()
    o.side.value = side
    o.order_type = MagicMock()
    o.order_type.value = order_type
    return o


def _alpaca_position(*, symbol: str, qty: float = 1.0):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _make_client(
    *,
    get_order_by_id_return=None,
    get_orders_return: list | None = None,
    held_tickers: Iterable[str] = (),
):
    """Build a MagicMock Alpaca client with the three methods the repair
    script uses pre-wired. ``held_tickers`` controls
    ``get_all_positions`` so tests can assert the
    filled-but-not-held branch."""
    client = MagicMock()
    if get_order_by_id_return is not None:
        client.get_order_by_id = MagicMock(return_value=get_order_by_id_return)
    client.get_orders = MagicMock(return_value=get_orders_return or [])
    client.get_all_positions = MagicMock(
        return_value=[_alpaca_position(symbol=t) for t in held_tickers]
    )
    return client


def _seed_position(
    db_path: str, *, ticker: str, status: str, strategy_id: str,
    alpaca_order_id: str | None = None, qty: float = 1.0,
    entry_price: float = 100.0,
    entry_time: str = "2026-05-05T15:45:00",
) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO positions (ticker, exchange, currency, "
            "quantity, entry_price, entry_time, status, hold_type, "
            "phase, strategy_id, alpaca_order_id) VALUES "
            "(?, 'US', 'USD', ?, ?, ?, ?, "
            "'overnight', 1, ?, ?)",
            (ticker, qty, entry_price, entry_time, status,
             strategy_id, alpaca_order_id),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 1: ENTRY_FAILED rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flips_to_active_when_filled_and_held_with_stop(tmp_db_path: str):
    pos_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1", qty=0.204, entry_price=720.0,
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(
            status="filled", filled_qty=0.204, filled_avg_price=724.72,
        ),
        get_orders_return=[
            _alpaca_open_stop(order_id="stop-recovered",
                             symbol="SPY", qty=0.204),
        ],
        held_tickers=["SPY"],
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    assert report.entry_failed_scanned == 1
    assert report.entry_failed_repaired == 1
    assert report.entry_failed_marked_closed == 0

    conn = sqlite3.connect(tmp_db_path)
    row = conn.execute(
        "SELECT status, entry_price, alpaca_stop_order_id "
        "FROM positions WHERE id = ?", (pos_id,),
    ).fetchone()
    conn.close()
    assert row[0] == PositionStatus.STOP_AND_TARGET_ACTIVE.value
    assert abs(row[1] - 724.72) < 1e-6
    assert row[2] == "stop-recovered"


@pytest.mark.asyncio
async def test_flips_to_position_open_when_held_but_no_stop(tmp_db_path: str):
    pos_id = _seed_position(
        tmp_db_path, ticker="QQQ",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-2", qty=0.1482, entry_price=680.0,
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(
            status="filled", filled_qty=0.1482, filled_avg_price=682.07,
        ),
        get_orders_return=[],
        held_tickers=["QQQ"],
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    conn = sqlite3.connect(tmp_db_path)
    row = conn.execute(
        "SELECT status, alpaca_stop_order_id FROM positions WHERE id = ?",
        (pos_id,),
    ).fetchone()
    conn.close()
    assert row[0] == PositionStatus.POSITION_OPEN.value
    assert row[1] is None or row[1] == ""


@pytest.mark.asyncio
async def test_marks_closed_when_filled_but_not_held(tmp_db_path: str):
    """Live bug from 2026-05-06: positions 70/71/74/75 had entry orders
    that filled on 5/4 but were also exited the same day. The previous
    repair flipped them to POSITION_OPEN as ghost rows. The fix:
    when filled but not currently held, mark CLOSED with a note —
    never create a ghost."""
    pos_id = _seed_position(
        tmp_db_path, ticker="XLY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="mean_reversion",
        alpaca_order_id="entry-roundtripped",
        qty=4.2067, entry_price=117.5,
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(
            status="filled", filled_qty=4.2067, filled_avg_price=117.67,
        ),
        # XLY filled but not currently held — round-tripped before repair
        held_tickers=["SPY", "QQQ"],
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    assert report.entry_failed_scanned == 1
    assert report.entry_failed_repaired == 0, (
        "must NOT flip to live — that's the bug we're fixing"
    )
    assert report.entry_failed_marked_closed == 1

    conn = sqlite3.connect(tmp_db_path)
    row = conn.execute(
        "SELECT status, entry_price FROM positions WHERE id = ?",
        (pos_id,),
    ).fetchone()
    conn.close()
    assert row[0] == PositionStatus.CLOSED.value
    # entry_price still gets backfilled from the actual broker fill so
    # daily-review can match this position to its exit in the trades
    # table later.
    assert abs(row[1] - 117.67) < 1e-6


@pytest.mark.asyncio
async def test_leaves_genuine_failures_alone(tmp_db_path: str):
    pos_id = _seed_position(
        tmp_db_path, ticker="XLP",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="mean_reversion",
        alpaca_order_id="entry-canceled",
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(status="canceled"),
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (pos_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.entry_failed_repaired == 0
    assert report.entry_failed_marked_closed == 0
    assert row[0] == PositionStatus.ENTRY_FAILED.value


# ---------------------------------------------------------------------------
# Step 2: unknown-strategy duplicates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_closes_unknown_duplicate_when_real_sibling_exists(
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
        entry_time="2026-05-05T15:50:00",  # later than real_id
    )
    # SPY is held — phantom step won't touch the live real_id.
    client = _make_client(held_tickers=["SPY"])

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
    assert rows_by_id[unk_id][1] == "CLOSED"


@pytest.mark.asyncio
async def test_leaves_unknown_alone_when_no_real_sibling(tmp_db_path: str):
    """An unknown-strategy POSITION_OPEN with no sibling AND ticker is
    held at Alpaca: don't close — could be a genuine broker-side-only
    position the bot didn't open. The phantom step should also leave it
    alone because the ticker is held."""
    unk_id = _seed_position(
        tmp_db_path, ticker="MSFT",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="unknown",
    )
    client = _make_client(held_tickers=["MSFT"])

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


# ---------------------------------------------------------------------------
# Step 3: phantom live rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phantom_step_closes_ghost_rows_for_unheld_tickers(
    tmp_db_path: str,
):
    """Live bug: a previous repair pass flipped position 70 (XLY) from
    ENTRY_FAILED to POSITION_OPEN even though Alpaca no longer held
    XLY. The ghost row sits in the DB as a phantom. The phantom step
    must close it."""
    ghost_id = _seed_position(
        tmp_db_path, ticker="XLY",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="mean_reversion",
        alpaca_order_id="entry-1",
    )
    client = _make_client(held_tickers=[])  # XLY not held

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?",
            (ghost_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.phantom_live_closed == 1
    assert row[0] == PositionStatus.CLOSED.value


@pytest.mark.asyncio
async def test_phantom_step_closes_older_duplicate_keeps_latest(
    tmp_db_path: str,
):
    """Live bug: positions 74 (SPY 5/4) and 80 (SPY 5/5) both
    POSITION_OPEN/STOP_AND_TARGET_ACTIVE for the same held ticker.
    The reconciler's db_by_ticker dict overwrites duplicates and
    silently drops one — so the older row never gets closed.

    The phantom step must close the OLDER row and keep the latest
    one matching the actual held position.
    """
    older_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-04T15:45:00",  # older
    )
    newer_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.STOP_AND_TARGET_ACTIVE.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-05T15:45:00",  # newer
    )
    client = _make_client(held_tickers=["SPY"])

    conn = sqlite3.connect(tmp_db_path)
    try:
        await repair(conn, client, dry_run=False)
        rows = conn.execute(
            "SELECT id, status FROM positions WHERE ticker='SPY' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    by_id = {r[0]: r[1] for r in rows}
    assert by_id[older_id] == PositionStatus.CLOSED.value, (
        "older duplicate must be closed — it can't all map to the one "
        "broker-side position"
    )
    assert by_id[newer_id] == PositionStatus.STOP_AND_TARGET_ACTIVE.value, (
        "newer row must be preserved — it matches the broker"
    )


@pytest.mark.asyncio
async def test_phantom_step_leaves_single_held_row_alone(tmp_db_path: str):
    """The most common case: one DB row per held ticker. Phantom step
    must not touch it."""
    pos_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.STOP_AND_TARGET_ACTIVE.value,
        strategy_id="overnight_drift",
    )
    client = _make_client(held_tickers=["SPY"])

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (pos_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.phantom_live_closed == 0
    assert row[0] == PositionStatus.STOP_AND_TARGET_ACTIVE.value


# ---------------------------------------------------------------------------
# Cross-cutting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dry_run_makes_no_writes(tmp_db_path: str):
    pos_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1", qty=0.5, entry_price=700.0,
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(
            status="filled", filled_qty=0.5, filled_avg_price=720.0,
        ),
        held_tickers=["SPY"],
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=True)
        row = conn.execute(
            "SELECT status, entry_price FROM positions WHERE id = ?",
            (pos_id,),
        ).fetchone()
    finally:
        conn.close()

    assert report.entry_failed_repaired == 1
    assert row[0] == PositionStatus.ENTRY_FAILED.value
    assert abs(row[1] - 700.0) < 1e-6


@pytest.mark.asyncio
async def test_idempotent(tmp_db_path: str):
    _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.ENTRY_FAILED.value,
        strategy_id="overnight_drift",
        alpaca_order_id="entry-1", qty=0.2, entry_price=700.0,
    )
    client = _make_client(
        get_order_by_id_return=_alpaca_order(
            status="filled", filled_qty=0.2, filled_avg_price=720.0,
        ),
        held_tickers=["SPY"],
    )

    conn = sqlite3.connect(tmp_db_path)
    try:
        first = await repair(conn, client, dry_run=False)
        second = await repair(conn, client, dry_run=False)
    finally:
        conn.close()

    assert first.entry_failed_repaired == 1
    assert second.entry_failed_repaired == 0
    assert second.phantom_live_closed == 0


@pytest.mark.asyncio
async def test_full_scenario_2026_05_06_repair_state(tmp_db_path: str):
    """End-to-end: seed the actual 2026-05-06 mid-repair state and
    confirm the new logic cleans it up correctly in a single pass.

    Live held: SPY, QQQ, XLC, XLK (4 positions kept their stop adoption).
    Ghost rows: XLY/XLB (filled then exited 5/4) and older SPY/QQQ
    duplicates from 5/4 sitting alongside the held 5/5 entries.
    """
    # Already-flipped ghosts from the bad first repair pass.
    xly_id = _seed_position(
        tmp_db_path, ticker="XLY",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="mean_reversion",
        entry_time="2026-05-04T12:20:31",
    )
    xlb_id = _seed_position(
        tmp_db_path, ticker="XLB",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="mean_reversion",
        entry_time="2026-05-04T12:20:32",
    )
    spy_old_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-04T15:45:37",
    )
    qqq_old_id = _seed_position(
        tmp_db_path, ticker="QQQ",
        status=PositionStatus.POSITION_OPEN.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-04T15:45:38",
    )
    # Real held positions from 5/5.
    spy_real_id = _seed_position(
        tmp_db_path, ticker="SPY",
        status=PositionStatus.STOP_AND_TARGET_ACTIVE.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-05T15:45:36",
    )
    qqq_real_id = _seed_position(
        tmp_db_path, ticker="QQQ",
        status=PositionStatus.STOP_AND_TARGET_ACTIVE.value,
        strategy_id="overnight_drift",
        entry_time="2026-05-05T15:45:36",
    )

    client = _make_client(held_tickers=["SPY", "QQQ", "XLC", "XLK"])

    conn = sqlite3.connect(tmp_db_path)
    try:
        report = await repair(conn, client, dry_run=False)
        rows = conn.execute(
            "SELECT id, ticker, status FROM positions ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    by_id = {r[0]: (r[1], r[2]) for r in rows}
    assert by_id[xly_id][1] == PositionStatus.CLOSED.value
    assert by_id[xlb_id][1] == PositionStatus.CLOSED.value
    assert by_id[spy_old_id][1] == PositionStatus.CLOSED.value
    assert by_id[qqq_old_id][1] == PositionStatus.CLOSED.value
    assert by_id[spy_real_id][1] == PositionStatus.STOP_AND_TARGET_ACTIVE.value
    assert by_id[qqq_real_id][1] == PositionStatus.STOP_AND_TARGET_ACTIVE.value
    assert report.phantom_live_closed == 4

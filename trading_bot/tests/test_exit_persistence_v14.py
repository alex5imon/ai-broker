"""V14 cross-tick exit-reason persistence + ``_close_position`` writes ``pnl_usd``.

Companion to V11 (alpaca_exit_order_id). Pre-V14 the strategy-supplied
``reason`` for an in-flight strategy exit lived only on the in-memory
``_ActiveOrder.exit_reason``. Because each stateless tick rebuilds
``_active_orders`` from ``positions``, the reason vaporized between
the tick that placed the exit and the tick that observed the fill —
the fill-detection branch then defaulted to ``"strategy_exit"``.

Regression observed 2026-05-18: both SPY and QQQ overnight_drift
exits (held over Friday's close) wrote ``exit_reason='strategy_exit'``
into trades instead of the correct ``'overnight_exit'``.

Tests pin:

1. ``place_exit`` persists ``exit_reason`` to the positions row.
2. ``_hydrate_active_orders`` reads it back into ``_ActiveOrder``.
3. Rollback (cancel/expire/reject) clears the persisted column.
4. ``_close_position`` writes ``pnl_usd`` alongside ``net_pnl`` —
   downstream daily-loss / daily-summary readers depend on
   ``pnl_usd`` and silently treat NULL as zero.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.execution.order_manager import (
    EntryDecision,
    OrderManager,
    _ActiveOrder,
)

pytestmark = pytest.mark.critical

ET = TZ_EASTERN


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _entry(ticker: str = "SPY") -> EntryDecision:
    return EntryDecision(
        ticker=ticker,
        exchange="US",
        side="BUY",
        shares=10,
        limit_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        hold_type="intraday",
        sector="Information Technology",
        phase=1,
        sentiment_score=0.2,
        signals="test",
        currency="USD",
        strategy_id="overnight_drift",
        trail_pct=None,
        trail_activation_price=None,
    )


def _seed_open_position(om: OrderManager, ticker: str = "SPY") -> int:
    trade_id = om._create_position_record(_entry(ticker))
    om._active_orders[trade_id] = _ActiveOrder(
        trade_id=trade_id,
        ticker=ticker,
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
        strategy_id="overnight_drift",
        db_trade_id=trade_id,
    )
    om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
    return trade_id


def _stub_alpaca_exit_submit(om: OrderManager, exit_id: str = "exit-1") -> None:
    om._gw.client.get_orders = MagicMock(return_value=[])
    om._gw.client.cancel_order_by_id = MagicMock()
    submitted = MagicMock()
    submitted.id = exit_id
    om._gw.client.submit_order = MagicMock(return_value=submitted)


@pytest.mark.asyncio
async def test_place_exit_persists_exit_reason(
    config, tmp_db_path: str, mock_notifier,
):
    """After successful place_exit, positions.exit_reason holds the
    strategy-supplied reason — not just the in-memory _ActiveOrder.
    This is the invariant that survives the tick boundary between
    place_exit and fill detection."""
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_position(om)
    _stub_alpaca_exit_submit(om, exit_id="alp-exit-overnight")

    order_id = await om.place_exit(
        ticker="SPY", qty=10, reason="overnight_exit",
    )
    assert order_id == "alp-exit-overnight"
    assert om._active_orders[trade_id].exit_reason == "overnight_exit"

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT exit_reason FROM positions WHERE id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "overnight_exit", (
        "place_exit must persist exit_reason so the fill-detection "
        "tick can rehydrate it instead of defaulting to strategy_exit"
    )


@pytest.mark.asyncio
async def test_hydrate_recovers_exit_reason_after_restart(
    config, tmp_db_path: str, mock_notifier,
):
    """Simulate the tick boundary: place_exit on OM_a, then build OM_b
    against the same DB. The new OM must rehydrate exit_reason."""
    om_a = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_position(om_a)
    _stub_alpaca_exit_submit(om_a, exit_id="alp-exit-rehydrate")
    await om_a.place_exit(ticker="SPY", qty=10, reason="overnight_exit")

    om_b = _make_om(config, tmp_db_path, mock_notifier)
    assert om_b._active_orders == {}
    om_b._hydrate_active_orders()

    active = om_b._active_orders[trade_id]
    assert active.exit_reason == "overnight_exit", (
        "hydration must restore the persisted exit_reason so fill "
        "detection writes 'overnight_exit' to trades, not 'strategy_exit'"
    )
    assert active.alpaca_exit_order_id == "alp-exit-rehydrate"
    assert active.status == PositionStatus.CLOSING


@pytest.mark.asyncio
async def test_cancel_rollback_clears_persisted_exit_reason(
    config, tmp_db_path: str, mock_notifier,
):
    """On cancel/expire/reject the persisted exit_reason must also be
    cleared, mirroring the alpaca_exit_order_id rollback. Otherwise the
    next place_exit could race ahead of the new reason assignment and
    the fill would inherit the stale value."""
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_position(om)
    _stub_alpaca_exit_submit(om, exit_id="alp-exit-to-cancel")
    await om.place_exit(ticker="SPY", qty=10, reason="overnight_exit")

    conn = sqlite3.connect(tmp_db_path)
    try:
        assert conn.execute(
            "SELECT exit_reason FROM positions WHERE id = ?",
            (trade_id,),
        ).fetchone()[0] == "overnight_exit"
    finally:
        conn.close()

    canceled = MagicMock()
    canceled.id = "alp-exit-to-cancel"
    canceled.status = MagicMock()
    canceled.status.value = "canceled"
    canceled.filled_qty = 0
    canceled.filled_avg_price = 0
    om._gw.client.get_order_by_id = MagicMock(return_value=canceled)
    om._gw.client.get_orders = MagicMock(return_value=[])

    await om._check_order_statuses()

    assert om._active_orders[trade_id].exit_reason is None
    assert om._active_orders[trade_id].alpaca_exit_order_id is None
    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT exit_reason, alpaca_exit_order_id FROM positions WHERE id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] is None, "DB column must be cleared on rollback"
    assert row[1] is None


@pytest.mark.asyncio
async def test_close_position_writes_pnl_usd(
    config, tmp_db_path: str, mock_notifier,
):
    """``_close_position`` must populate ``trades.pnl_usd`` in lockstep
    with ``net_pnl``. Downstream readers (daily-loss circuit breaker,
    performance.calculate_daily_metrics, _save_daily_summary) all key
    off ``pnl_usd`` and silently treat NULL as zero. The 2026-05-18
    regression stamped four trades with pnl_usd=NULL — the daily
    summary then reported wins=0/losses=0 despite three real losers
    and one real winner, and the daily-loss circuit saw $0 P&L.
    """
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_position(om)
    _stub_alpaca_exit_submit(om, exit_id="alp-exit-pnl")
    await om.place_exit(ticker="SPY", qty=10, reason="overnight_exit")

    # Simulate broker reporting the exit FILLED via _check_order_statuses.
    filled = MagicMock()
    filled.id = "alp-exit-pnl"
    filled.status = MagicMock()
    filled.status.value = "filled"
    filled.filled_qty = 10
    filled.filled_avg_price = 99.50  # 0.50 loss/share × 10 = -5.00
    om._gw.client.get_order_by_id = MagicMock(return_value=filled)
    om._gw.client.get_orders = MagicMock(return_value=[])

    await om._check_order_statuses()

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT net_pnl, pnl_usd, exit_reason "
            "FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "trades row must exist after close"
    net_pnl, pnl_usd, exit_reason = row
    assert net_pnl == pytest.approx(-5.0)
    assert pnl_usd is not None, (
        "pnl_usd must be written by _close_position — leaving it NULL "
        "breaks the daily-loss circuit and daily summaries"
    )
    assert pnl_usd == pytest.approx(net_pnl)
    assert exit_reason == "overnight_exit", (
        "exit_reason must propagate through the fill-detection path"
    )

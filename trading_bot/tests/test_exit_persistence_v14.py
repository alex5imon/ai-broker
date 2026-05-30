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


# ---------------------------------------------------------------------------
# _signed_pnl unit tests (issue #126)
# ---------------------------------------------------------------------------


class TestSignedPnl:
    """Unit tests for the _signed_pnl helper (issue #126).

    All current production strategies are long-only (side="BUY"), so the
    short branch has no production caller today.  These tests give the
    branch an explicit specification so that:
      (a) regressions on the long path are caught immediately, and
      (b) the short branch can be trusted when a short-side strategy lands
          without needing to re-derive the formula from first principles.
    """

    from trading_bot.execution.order_manager import _signed_pnl as _sp

    def test_long_profit_when_exit_above_entry(self) -> None:
        from trading_bot.execution.order_manager import _signed_pnl
        pnl = _signed_pnl(entry_price=100.0, exit_price=105.0, qty=10.0, side="BUY")
        assert pnl == pytest.approx(50.0)

    def test_long_loss_when_exit_below_entry(self) -> None:
        from trading_bot.execution.order_manager import _signed_pnl
        pnl = _signed_pnl(entry_price=100.0, exit_price=95.0, qty=10.0, side="BUY")
        assert pnl == pytest.approx(-50.0)

    def test_long_breakeven(self) -> None:
        from trading_bot.execution.order_manager import _signed_pnl
        assert _signed_pnl(100.0, 100.0, 5.0, "BUY") == pytest.approx(0.0)

    def test_long_fractional_shares(self) -> None:
        from trading_bot.execution.order_manager import _signed_pnl
        # 0.5 shares, $2 gain/share → $1.00 P&L
        assert _signed_pnl(100.0, 102.0, 0.5, "BUY") == pytest.approx(1.0)

    def test_short_profit_when_exit_below_entry(self) -> None:
        """A short profits when we buy back lower than we sold."""
        from trading_bot.execution.order_manager import _signed_pnl
        pnl = _signed_pnl(entry_price=100.0, exit_price=90.0, qty=10.0, side="SELL")
        assert pnl == pytest.approx(100.0), (
            "Short: sold at 100, bought back at 90, 10 shares → +$100"
        )

    def test_short_loss_when_exit_above_entry(self) -> None:
        """A short loses when price rises above the entry short price."""
        from trading_bot.execution.order_manager import _signed_pnl
        pnl = _signed_pnl(entry_price=100.0, exit_price=110.0, qty=10.0, side="SELL")
        assert pnl == pytest.approx(-100.0), (
            "Short: sold at 100, bought back at 110, 10 shares → -$100"
        )

    def test_short_breakeven(self) -> None:
        from trading_bot.execution.order_manager import _signed_pnl
        assert _signed_pnl(100.0, 100.0, 5.0, "SELL") == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Integration: _close_position with side="BUY" still correct (regression)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_close_position_long_pnl_sign_unchanged(
    config, tmp_db_path: str, mock_notifier,
) -> None:
    """Regression guard: adding side field must not change long P&L sign."""
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_position(om)  # side defaults to "BUY" in _entry()
    _stub_alpaca_exit_submit(om, exit_id="alp-long-sign-check")
    await om.place_exit(ticker="SPY", qty=10, reason="overnight_exit")

    filled = MagicMock()
    filled.id = "alp-long-sign-check"
    filled.status = MagicMock()
    filled.status.value = "filled"
    filled.filled_qty = 10
    filled.filled_avg_price = 105.0  # entry=100 → +5/share → +50 total
    om._gw.client.get_order_by_id = MagicMock(return_value=filled)
    om._gw.client.get_orders = MagicMock(return_value=[])

    await om._check_order_statuses()

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT net_pnl FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == pytest.approx(50.0), (
        "Long position: exit 105 > entry 100, 10 shares → +$50"
    )


# ---------------------------------------------------------------------------
# Integration: _close_position with side="SELL" (short) — issue #126 core fix
# ---------------------------------------------------------------------------


def _entry_short(ticker: str = "SPY") -> EntryDecision:
    """EntryDecision for a short position."""
    return EntryDecision(
        ticker=ticker,
        exchange="US",
        side="SELL",
        shares=10,
        limit_price=100.0,
        stop_price=104.0,   # stop above entry (short stop)
        target_price=96.0,  # target below entry (short target)
        hold_type="intraday",
        sector="Information Technology",
        phase=1,
        sentiment_score=None,
        signals="test-short",
        currency="USD",
        strategy_id="test_short_strategy",
        trail_pct=None,
        trail_activation_price=None,
    )


def _seed_open_short(om: OrderManager, ticker: str = "SPY") -> int:
    trade_id = om._create_position_record(_entry_short(ticker))
    om._active_orders[trade_id] = _ActiveOrder(
        trade_id=trade_id,
        ticker=ticker,
        exchange="US",
        alpaca_entry_order_id="entry-short-1",
        status=PositionStatus.STOP_ACTIVE,
        side="SELL",
        entry_shares=10.0,
        filled_shares=10.0,
        entry_price=100.0,
        stop_price=104.0,
        target_price=96.0,
        hold_type="intraday",
        strategy_id="test_short_strategy",
        db_trade_id=trade_id,
    )
    om._update_position_status(trade_id, PositionStatus.STOP_ACTIVE)
    return trade_id


@pytest.mark.asyncio
async def test_close_position_short_profit_sign_correct(
    config, tmp_db_path: str, mock_notifier,
) -> None:
    """Core fix for issue #126: closing a short at a price BELOW entry
    must record a POSITIVE P&L.  Pre-fix: (exit - entry) * qty gave -$50
    (inverted sign).  Post-fix: (entry - exit) * qty gives +$50.
    """
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_short(om)
    _stub_alpaca_exit_submit(om, exit_id="alp-short-profit")
    await om.place_exit(ticker="SPY", qty=10, reason="target_hit")

    filled = MagicMock()
    filled.id = "alp-short-profit"
    filled.status = MagicMock()
    filled.status.value = "filled"
    filled.filled_qty = 10
    filled.filled_avg_price = 95.0  # shorted at 100, covered at 95 → +5/share
    om._gw.client.get_order_by_id = MagicMock(return_value=filled)
    om._gw.client.get_orders = MagicMock(return_value=[])

    await om._check_order_statuses()

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT net_pnl FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == pytest.approx(50.0), (
        "Short: sold at 100, covered at 95, 10 shares → +$50. "
        "Pre-fix this was -$50 (inverted sign, issue #126)."
    )


@pytest.mark.asyncio
async def test_close_position_short_loss_sign_correct(
    config, tmp_db_path: str, mock_notifier,
) -> None:
    """Short position stopped out above entry → loss must be negative."""
    om = _make_om(config, tmp_db_path, mock_notifier)
    trade_id = _seed_open_short(om)
    _stub_alpaca_exit_submit(om, exit_id="alp-short-loss")
    await om.place_exit(ticker="SPY", qty=10, reason="stop_loss")

    filled = MagicMock()
    filled.id = "alp-short-loss"
    filled.status = MagicMock()
    filled.status.value = "filled"
    filled.filled_qty = 10
    filled.filled_avg_price = 103.0  # shorted at 100, stopped at 103 → -3/share
    om._gw.client.get_order_by_id = MagicMock(return_value=filled)
    om._gw.client.get_orders = MagicMock(return_value=[])

    await om._check_order_statuses()

    conn = sqlite3.connect(tmp_db_path)
    try:
        row = conn.execute(
            "SELECT net_pnl FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == pytest.approx(-30.0), (
        "Short: sold at 100, stopped at 103, 10 shares → -$30."
    )


@pytest.mark.asyncio
async def test_hydrate_reads_side_from_positions_table(
    config, tmp_db_path: str, mock_notifier,
) -> None:
    """``_hydrate_active_orders`` must populate ``_ActiveOrder.side`` from
    the ``positions.side`` column so a restarted tick uses the correct P&L
    sign for any held short position (issue #126).
    """
    # Seed a short position in the DB directly (simulates a held short
    # surviving a tick boundary after a tick crash post-entry).
    import sqlite3 as _sqlite3
    from datetime import datetime
    from zoneinfo import ZoneInfo
    conn = _sqlite3.connect(tmp_db_path)
    try:
        conn.row_factory = _sqlite3.Row
        now_str = datetime.now(tz=ZoneInfo("US/Eastern")).isoformat()
        conn.execute(
            "INSERT INTO positions "
            "(ticker, exchange, currency, side, quantity, entry_price, entry_time, "
            " stop_price, target_price, status, hold_type, phase, strategy_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("QQQ", "US", "USD", "SELL", 5, 200.0, now_str,
             205.0, 194.0, "POSITION_OPEN", "intraday", 1, "test_short_strategy"),
        )
        conn.commit()
    finally:
        conn.close()

    om = _make_om(config, tmp_db_path, mock_notifier)
    om._hydrate_active_orders()

    # Should have exactly one active order — the QQQ short.
    assert len(om._active_orders) == 1
    active = next(iter(om._active_orders.values()))
    assert active.ticker == "QQQ"
    assert active.side == "SELL", (
        "_hydrate_active_orders must read positions.side so a restarted "
        "tick preserves the correct P&L sign for held shorts (issue #126)."
    )

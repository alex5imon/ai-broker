"""Tests for the Phase 2 reconciler convergence engine (PR #191 design).

Phase 2 lets the reconciler OWN the DB ``status`` column for the lossless,
DB-only transitions it can safely drive from positive broker evidence, gated
by the ``reconciler.drive`` flag. Order-submitting / P&L-bearing actions are
planned + logged but delegated to the existing healers.

Covers:
- :func:`plan_convergence` — pure mapping from dispositions to typed actions;
  which ops are reconciler-owned vs delegated.
- :func:`converge` — drive OFF executes nothing; drive ON executes ONLY the
  lossless ``NORMALIZE_STATUS`` write; order-bearing ops are never executed.
- :func:`_write_status` — refuses to touch a terminal row.
- :func:`run_reconcile` — end-to-end drive vs shadow over a temp DB + mock
  gateway, asserting the DB status is/ isn't changed and no order is placed.
- Safety: a transient/empty broker read can never drive a normalization.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.execution.reconciler import (
    ConvergenceOp,
    DesiredState,
    Disposition,
    _write_status,
    converge,
    plan_convergence,
    run_reconcile,
)

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _alpaca_position(symbol: str, qty: float):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _alpaca_order(symbol: str, order_type: str):
    o = MagicMock()
    o.symbol = symbol
    o.type = MagicMock()
    o.type.value = order_type
    return o


def _disp(
    ticker: str,
    intent_status: str | None,
    desired: DesiredState,
    *,
    trade_id: int | None = 1,
) -> Disposition:
    return Disposition(
        ticker=ticker,
        intent_status=intent_status,
        desired=desired,
        action="",
        agrees_with_db=False,
        trade_id=trade_id,
    )


def _insert_position(db_path: str, ticker: str, status: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO positions "
            "(ticker, exchange, currency, quantity, entry_price, entry_time, "
            "status, hold_type, phase, strategy_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ticker, "US", "USD", 1.0, 100.0,
                "2026-06-04T10:00:00-04:00", status, "intraday", 3,
                "mean_reversion",
            ),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def _status_of(db_path: str, trade_id: int) -> str:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT status FROM positions WHERE id = ?", (trade_id,)
        ).fetchone()
        return str(row[0]) if row else ""
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# plan_convergence — pure
# ---------------------------------------------------------------------------


class TestPlanConvergence:
    def test_protected_open_normalizes_and_is_reconciler_owned(self) -> None:
        disp = _disp(
            "SPY", PositionStatus.POSITION_OPEN.value, DesiredState.PROTECTED
        )
        plan = plan_convergence([disp])
        assert len(plan) == 1
        assert plan[0].op == ConvergenceOp.NORMALIZE_STATUS
        assert plan[0].target_status == PositionStatus.STOP_ACTIVE.value
        assert plan[0].reconciler_owned is True

    def test_protected_already_stop_active_is_noop_dropped(self) -> None:
        disp = _disp(
            "SPY", PositionStatus.STOP_ACTIVE.value, DesiredState.PROTECTED
        )
        # NONE actions are dropped from the plan.
        assert plan_convergence([disp]) == []

    @pytest.mark.parametrize(
        "desired,expected_op",
        [
            (DesiredState.NEEDS_STOP, ConvergenceOp.ATTACH_STOP),
            (DesiredState.FLATTEN, ConvergenceOp.FLATTEN),
            (DesiredState.CLOSED, ConvergenceOp.JOURNAL_CLOSED),
            (DesiredState.OPEN, ConvergenceOp.ADOPT),
            (DesiredState.ADOPT, ConvergenceOp.ADOPT),
            (DesiredState.UNKNOWN, ConvergenceOp.INVESTIGATE),
        ],
    )
    def test_order_bearing_ops_are_delegated(
        self, desired: DesiredState, expected_op: ConvergenceOp
    ) -> None:
        disp = _disp("SPY", PositionStatus.POSITION_OPEN.value, desired)
        plan = plan_convergence([disp])
        assert len(plan) == 1
        assert plan[0].op == expected_op
        assert plan[0].reconciler_owned is False

    def test_reconciler_owned_actions_sorted_first(self) -> None:
        plan = plan_convergence([
            _disp("QQQ", PositionStatus.STOP_ACTIVE.value, DesiredState.CLOSED),
            _disp("SPY", PositionStatus.POSITION_OPEN.value, DesiredState.PROTECTED),
        ])
        assert [a.ticker for a in plan] == ["SPY", "QQQ"]
        assert plan[0].reconciler_owned is True


# ---------------------------------------------------------------------------
# converge — execution gating
# ---------------------------------------------------------------------------


class TestConverge:
    @pytest.mark.asyncio
    async def test_drive_off_executes_nothing(self, tmp_db_path: str) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        plan = plan_convergence([
            _disp("SPY", PositionStatus.POSITION_OPEN.value,
                  DesiredState.PROTECTED, trade_id=tid),
        ])
        executed = await converge(tmp_db_path, plan, drive=False)
        assert executed == []
        assert _status_of(tmp_db_path, tid) == PositionStatus.POSITION_OPEN.value

    @pytest.mark.asyncio
    async def test_drive_on_normalizes_status(self, tmp_db_path: str) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        plan = plan_convergence([
            _disp("SPY", PositionStatus.POSITION_OPEN.value,
                  DesiredState.PROTECTED, trade_id=tid),
        ])
        executed = await converge(tmp_db_path, plan, drive=True)
        assert len(executed) == 1
        assert _status_of(tmp_db_path, tid) == PositionStatus.STOP_ACTIVE.value

    @pytest.mark.asyncio
    async def test_drive_on_does_not_execute_order_bearing_ops(
        self, tmp_db_path: str
    ) -> None:
        tid = _insert_position(
            tmp_db_path, "QQQ", PositionStatus.STOP_ACTIVE.value
        )
        # Orphan -> CLOSED -> JOURNAL_CLOSED (delegated, not reconciler-owned).
        plan = plan_convergence([
            _disp("QQQ", PositionStatus.STOP_ACTIVE.value,
                  DesiredState.CLOSED, trade_id=tid),
        ])
        executed = await converge(tmp_db_path, plan, drive=True)
        assert executed == []
        # Status untouched — the reconciler did not journal the close itself.
        assert _status_of(tmp_db_path, tid) == PositionStatus.STOP_ACTIVE.value


# ---------------------------------------------------------------------------
# _write_status — terminal-row guard
# ---------------------------------------------------------------------------


class TestWriteStatusGuard:
    def test_refuses_to_touch_terminal_row(self, tmp_db_path: str) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.CLOSED.value
        )
        wrote = _write_status(
            tmp_db_path, tid, PositionStatus.STOP_ACTIVE.value
        )
        assert wrote is False
        assert _status_of(tmp_db_path, tid) == PositionStatus.CLOSED.value

    def test_writes_non_terminal_row(self, tmp_db_path: str) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        wrote = _write_status(
            tmp_db_path, tid, PositionStatus.STOP_ACTIVE.value
        )
        assert wrote is True
        assert _status_of(tmp_db_path, tid) == PositionStatus.STOP_ACTIVE.value


# ---------------------------------------------------------------------------
# run_reconcile — end-to-end drive vs shadow
# ---------------------------------------------------------------------------


class TestRunReconcileDrive:
    @pytest.mark.asyncio
    async def test_shadow_does_not_change_db(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        mock_gateway.get_positions.return_value = [_alpaca_position("SPY", 1.0)]
        mock_gateway.get_open_orders.return_value = [_alpaca_order("SPY", "stop")]

        result = await run_reconcile(tmp_db_path, mock_gateway, drive=False)
        assert result.executed == []
        assert _status_of(tmp_db_path, tid) == PositionStatus.POSITION_OPEN.value
        mock_gateway.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_drive_owns_status_for_protected(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        # Held + broker stop on the book -> PROTECTED -> normalize.
        mock_gateway.get_positions.return_value = [_alpaca_position("SPY", 1.0)]
        mock_gateway.get_open_orders.return_value = [_alpaca_order("SPY", "stop")]

        result = await run_reconcile(tmp_db_path, mock_gateway, drive=True)
        assert len(result.executed) == 1
        assert _status_of(tmp_db_path, tid) == PositionStatus.STOP_ACTIVE.value
        # Still places NO orders — the only driven action is a DB status write.
        mock_gateway.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_drive_on_empty_broker_read_drives_nothing(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        # Safety: an empty/transient broker read yields no held positions, so
        # no PROTECTED disposition and no normalization can fire — even with
        # drive on. The orphan it produces is delegated (CLOSED), not driven.
        tid = _insert_position(
            tmp_db_path, "SPY", PositionStatus.POSITION_OPEN.value
        )
        mock_gateway.get_positions.return_value = []
        mock_gateway.get_open_orders.return_value = []

        result = await run_reconcile(tmp_db_path, mock_gateway, drive=True)
        assert result.executed == []
        assert _status_of(tmp_db_path, tid) == PositionStatus.POSITION_OPEN.value

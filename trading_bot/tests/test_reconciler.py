"""Tests for the Phase 1 shadow reconciler (PR #191 design, follows #192).

Covers:
- :func:`derive_desired_state` — one case per row of the design's §3
  disposition table (the pure derivation surface).
- :func:`derive_disposition` — the agreement flag (would the reconciler change
  the DB?) for the cases that matter.
- :func:`derive_all_dispositions` — ADOPT of unmatched broker holds + the
  disagreements-first ordering.
- :func:`run_shadow_reconcile` — read-only integration over a temp DB +
  mock gateway: takes no action, pages nothing, reports disagreements.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.execution.invariant_guard import BrokerView
from trading_bot.execution.reconciler import (
    DesiredState,
    PositionIntent,
    derive_all_dispositions,
    derive_desired_state,
    derive_disposition,
    run_shadow_reconcile,
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


def _broker(*, held: dict[str, float] | None = None, stops: list[str] | None = None):
    positions = [
        _alpaca_position(sym, qty) for sym, qty in (held or {}).items()
    ]
    orders = [_alpaca_order(sym, "stop") for sym in (stops or [])]
    return BrokerView.from_alpaca(positions, orders)


def _intent(ticker: str, status: str, qty: float = 1.0) -> PositionIntent:
    return PositionIntent(
        trade_id=1, ticker=ticker, status=status, quantity=qty,
        strategy_id="mean_reversion",
    )


# ---------------------------------------------------------------------------
# derive_desired_state — the §3 table
# ---------------------------------------------------------------------------


class TestDeriveDesiredState:
    def test_held_with_stop_open_is_protected(self) -> None:
        broker = _broker(held={"SPY": 1.0}, stops=["SPY"])
        intent = _intent("SPY", PositionStatus.STOP_ACTIVE.value)
        assert derive_desired_state(intent, broker) == DesiredState.PROTECTED

    def test_held_no_stop_open_is_needs_stop(self) -> None:
        broker = _broker(held={"SPY": 1.0})
        intent = _intent("SPY", PositionStatus.POSITION_OPEN.value)
        assert derive_desired_state(intent, broker) == DesiredState.NEEDS_STOP

    def test_closing_still_held_is_flatten(self) -> None:
        broker = _broker(held={"XLI": 0.5}, stops=["XLI"])
        intent = _intent("XLI", PositionStatus.CLOSING.value)
        assert derive_desired_state(intent, broker) == DesiredState.FLATTEN

    def test_closing_not_held_is_closed(self) -> None:
        broker = _broker()
        intent = _intent("XLI", PositionStatus.CLOSING.value)
        assert derive_desired_state(intent, broker) == DesiredState.CLOSED

    def test_open_not_held_is_closed(self) -> None:
        broker = _broker()
        intent = _intent("QQQ", PositionStatus.STOP_ACTIVE.value)
        assert derive_desired_state(intent, broker) == DesiredState.CLOSED

    def test_entry_pending_held_is_open(self) -> None:
        broker = _broker(held={"SPY": 1.0})
        intent = _intent("SPY", PositionStatus.ENTRY_PENDING.value)
        assert derive_desired_state(intent, broker) == DesiredState.OPEN

    def test_entry_pending_not_held_is_pending(self) -> None:
        broker = _broker()
        intent = _intent("SPY", PositionStatus.ENTRY_PENDING.value)
        assert derive_desired_state(intent, broker) == DesiredState.PENDING_ENTRY

    def test_unexpected_status_is_unknown(self) -> None:
        broker = _broker(held={"SPY": 1.0})
        intent = _intent("SPY", "WEIRD_STATUS")
        assert derive_desired_state(intent, broker) == DesiredState.UNKNOWN


# ---------------------------------------------------------------------------
# derive_disposition — agreement with the live DB
# ---------------------------------------------------------------------------


class TestAgreement:
    def test_protected_agrees_with_open_statuses(self) -> None:
        broker = _broker(held={"SPY": 1.0}, stops=["SPY"])
        for status in (
            PositionStatus.POSITION_OPEN.value,
            PositionStatus.STOP_ACTIVE.value,
            PositionStatus.TRAILING_ACTIVE.value,
        ):
            disp = derive_disposition(_intent("SPY", status), broker)
            assert disp.desired == DesiredState.PROTECTED
            assert disp.agrees_with_db is True

    def test_needs_stop_disagrees(self) -> None:
        # DB thinks it's protected (STOP_ACTIVE) but the broker has no stop.
        broker = _broker(held={"SPY": 1.0})
        disp = derive_disposition(
            _intent("SPY", PositionStatus.STOP_ACTIVE.value), broker
        )
        assert disp.desired == DesiredState.NEEDS_STOP
        assert disp.agrees_with_db is False

    def test_flatten_agrees_with_closing(self) -> None:
        # Intent already recorded as CLOSING — action pending, but DB reflects it.
        broker = _broker(held={"XLI": 1.0}, stops=["XLI"])
        disp = derive_disposition(
            _intent("XLI", PositionStatus.CLOSING.value), broker
        )
        assert disp.desired == DesiredState.FLATTEN
        assert disp.agrees_with_db is True

    def test_closed_disagrees(self) -> None:
        broker = _broker()
        disp = derive_disposition(
            _intent("QQQ", PositionStatus.STOP_ACTIVE.value), broker
        )
        assert disp.desired == DesiredState.CLOSED
        assert disp.agrees_with_db is False

    def test_pending_entry_agrees(self) -> None:
        broker = _broker()
        disp = derive_disposition(
            _intent("SPY", PositionStatus.ENTRY_PENDING.value), broker
        )
        assert disp.desired == DesiredState.PENDING_ENTRY
        assert disp.agrees_with_db is True


# ---------------------------------------------------------------------------
# derive_all_dispositions — adopt + ordering
# ---------------------------------------------------------------------------


class TestDeriveAll:
    def test_unmatched_broker_hold_is_adopted(self) -> None:
        broker = _broker(held={"XLE": 2.0})
        dispositions = derive_all_dispositions([], broker)
        assert len(dispositions) == 1
        assert dispositions[0].desired == DesiredState.ADOPT
        assert dispositions[0].ticker == "XLE"
        assert dispositions[0].intent_status is None
        assert dispositions[0].agrees_with_db is False

    def test_disagreements_sorted_first(self) -> None:
        # SPY protected (agree), QQQ orphan (disagree), XLE adopt (disagree).
        broker = _broker(held={"SPY": 1.0, "XLE": 1.0}, stops=["SPY"])
        intents = [
            _intent("SPY", PositionStatus.STOP_ACTIVE.value),
            _intent("QQQ", PositionStatus.STOP_ACTIVE.value),
        ]
        dispositions = derive_all_dispositions(intents, broker)
        # Disagreements (sorted by ticker) come before agreements.
        order = [(d.ticker, d.agrees_with_db) for d in dispositions]
        assert order == [("QQQ", False), ("XLE", False), ("SPY", True)]


# ---------------------------------------------------------------------------
# run_shadow_reconcile — read-only integration
# ---------------------------------------------------------------------------


def _insert_position(db_path: str, ticker: str, status: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
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
    finally:
        conn.close()


class TestShadowReconcile:
    @pytest.mark.asyncio
    async def test_all_consistent_no_disagreements(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        mock_gateway.get_positions.return_value = [_alpaca_position("SPY", 1.0)]
        mock_gateway.get_open_orders.return_value = [_alpaca_order("SPY", "stop")]

        result = await run_shadow_reconcile(tmp_db_path, mock_gateway)
        assert result.disagreements == []
        assert result.agreement_count == 1

    @pytest.mark.asyncio
    async def test_naked_position_flagged_as_disagreement(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        # DB says STOP_ACTIVE; broker holds it with no stop -> NEEDS_STOP.
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        mock_gateway.get_positions.return_value = [_alpaca_position("SPY", 1.0)]
        mock_gateway.get_open_orders.return_value = []

        result = await run_shadow_reconcile(tmp_db_path, mock_gateway)
        assert len(result.disagreements) == 1
        assert result.disagreements[0].desired == DesiredState.NEEDS_STOP

    @pytest.mark.asyncio
    async def test_takes_no_action_on_broker(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        # Orphan: DB open, broker flat. Phase 1 must NOT submit/cancel orders.
        _insert_position(
            tmp_db_path, "QQQ", PositionStatus.STOP_ACTIVE.value
        )
        mock_gateway.get_positions.return_value = []
        mock_gateway.get_open_orders.return_value = []

        result = await run_shadow_reconcile(tmp_db_path, mock_gateway)
        assert len(result.disagreements) == 1
        assert result.disagreements[0].desired == DesiredState.CLOSED
        # Read-only: no order placement / cancellation on the gateway.
        mock_gateway.client.submit_order.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_db_empty_broker(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        mock_gateway.get_positions.return_value = []
        mock_gateway.get_open_orders.return_value = []
        result = await run_shadow_reconcile(tmp_db_path, mock_gateway)
        assert result.dispositions == []
        assert result.disagreements == []

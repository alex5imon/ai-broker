"""Tests for the Phase 0 broker-truth invariant guard (PR #191 / #191 design).

Covers:
- :func:`BrokerView.from_alpaca` snapshot construction (qty epsilon, stop types).
- :func:`derive_violations` — the pure, side-effect-free derivation surface, one
  case per invariant (naked / wedge / orphan / unknown) plus the clean and
  legitimate-pending cases that must NOT flag.
- :func:`run_invariant_guard` — the >1-tick persistence gate: a fresh violation
  is logged but not paged; the same violation on a second consecutive tick
  escalates and notifies; a resolved violation clears the journal.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus
from trading_bot.execution.invariant_guard import (
    KIND_NAKED,
    KIND_ORPHAN,
    KIND_UNKNOWN,
    KIND_WEDGE,
    BrokerView,
    derive_violations,
    run_invariant_guard,
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


def _db_row(
    ticker: str,
    status: str,
    *,
    quantity: float = 1.0,
    strategy_id: str = "mean_reversion",
    trade_id: int = 1,
) -> dict[str, object]:
    return {
        "id": trade_id,
        "ticker": ticker,
        "status": status,
        "quantity": quantity,
        "strategy_id": strategy_id,
    }


# ---------------------------------------------------------------------------
# BrokerView.from_alpaca
# ---------------------------------------------------------------------------


class TestBrokerView:
    def test_sub_epsilon_position_treated_as_flat(self) -> None:
        view = BrokerView.from_alpaca(
            [_alpaca_position("SPY", 1e-9)], []
        )
        assert view.is_held("SPY") is False
        assert view.held_qty == {}

    def test_fractional_position_is_held(self) -> None:
        view = BrokerView.from_alpaca(
            [_alpaca_position("SPY", 0.3181)], []
        )
        assert view.is_held("SPY") is True
        assert view.held_qty["SPY"] == pytest.approx(0.3181)

    @pytest.mark.parametrize(
        "order_type,expected",
        [
            ("stop", True),
            ("stop_limit", True),
            ("trailing_stop", True),
            ("limit", False),
            ("market", False),
        ],
    )
    def test_protective_stop_detection(
        self, order_type: str, expected: bool
    ) -> None:
        view = BrokerView.from_alpaca(
            [_alpaca_position("SPY", 1.0)],
            [_alpaca_order("SPY", order_type)],
        )
        assert view.has_stop("SPY") is expected


# ---------------------------------------------------------------------------
# derive_violations — pure surface
# ---------------------------------------------------------------------------


class TestDeriveViolations:
    def test_clean_position_no_violation(self) -> None:
        broker = BrokerView.from_alpaca(
            [_alpaca_position("SPY", 1.0)],
            [_alpaca_order("SPY", "stop")],
        )
        rows = [_db_row("SPY", PositionStatus.STOP_ACTIVE.value)]
        assert derive_violations(broker, rows) == []

    def test_naked_position(self) -> None:
        # Broker holds it, but no protective stop on the book.
        broker = BrokerView.from_alpaca([_alpaca_position("SPY", 1.0)], [])
        rows = [_db_row("SPY", PositionStatus.POSITION_OPEN.value)]
        violations = derive_violations(broker, rows)
        assert len(violations) == 1
        assert violations[0].kind == KIND_NAKED
        assert violations[0].ticker == "SPY"

    def test_wedge_position(self) -> None:
        # DB says CLOSING, broker still holds it (the #190 wedge).
        broker = BrokerView.from_alpaca(
            [_alpaca_position("XLI", 0.5)],
            [_alpaca_order("XLI", "stop")],
        )
        rows = [_db_row("XLI", PositionStatus.CLOSING.value)]
        violations = derive_violations(broker, rows)
        assert len(violations) == 1
        assert violations[0].kind == KIND_WEDGE

    def test_orphan_position(self) -> None:
        # DB says open, broker does not hold it (the #65-#70 orphan).
        broker = BrokerView.from_alpaca([], [])
        rows = [_db_row("QQQ", PositionStatus.STOP_ACTIVE.value)]
        violations = derive_violations(broker, rows)
        assert len(violations) == 1
        assert violations[0].kind == KIND_ORPHAN

    def test_unknown_broker_position(self) -> None:
        # Broker holds a ticker the DB has no open row for.
        broker = BrokerView.from_alpaca([_alpaca_position("XLE", 2.0)], [])
        violations = derive_violations(broker, [])
        assert len(violations) == 1
        assert violations[0].kind == KIND_UNKNOWN
        assert violations[0].ticker == "XLE"

    def test_entry_pending_not_held_is_not_orphan(self) -> None:
        # ENTRY_PENDING has not filled yet — the broker legitimately does
        # not hold it. This must NOT be flagged as an orphan.
        broker = BrokerView.from_alpaca([], [])
        rows = [_db_row("SPY", PositionStatus.ENTRY_PENDING.value)]
        assert derive_violations(broker, rows) == []

    def test_terminal_rows_ignored(self) -> None:
        broker = BrokerView.from_alpaca([], [])
        rows = [
            _db_row("SPY", PositionStatus.CLOSED.value),
            _db_row("QQQ", PositionStatus.ENTRY_FAILED.value),
        ]
        assert derive_violations(broker, rows) == []

    def test_multiple_violations_sorted_deterministically(self) -> None:
        broker = BrokerView.from_alpaca(
            [
                _alpaca_position("SPY", 1.0),  # naked
                _alpaca_position("ZZZ", 1.0),  # unknown
            ],
            [],
        )
        rows = [
            _db_row("SPY", PositionStatus.POSITION_OPEN.value),
            _db_row("QQQ", PositionStatus.STOP_ACTIVE.value),  # orphan
        ]
        violations = derive_violations(broker, rows)
        kinds = [(v.kind, v.ticker) for v in violations]
        # Sorted by (kind, ticker): naked < orphan < unknown.
        assert kinds == [
            (KIND_NAKED, "SPY"),
            (KIND_ORPHAN, "QQQ"),
            (KIND_UNKNOWN, "ZZZ"),
        ]


# ---------------------------------------------------------------------------
# run_invariant_guard — persistence gate (integration with tick_state)
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
                ticker,
                "US",
                "USD",
                1.0,
                100.0,
                "2026-06-04T10:00:00-04:00",
                status,
                "intraday",
                3,
                "mean_reversion",
            ),
        )
        conn.commit()
    finally:
        conn.close()


class TestPersistenceGate:
    @pytest.mark.asyncio
    async def test_first_tick_does_not_escalate(
        self, tmp_db_path: str, mock_gateway, mock_notifier
    ) -> None:
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        # Broker does not hold SPY -> orphan, but it's the first sighting.
        result = await run_invariant_guard(
            tmp_db_path, mock_gateway, mock_notifier
        )
        assert result.has_violations
        assert result.escalated == []
        mock_notifier.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_second_consecutive_tick_escalates_and_pages(
        self, tmp_db_path: str, mock_gateway, mock_notifier
    ) -> None:
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        await run_invariant_guard(tmp_db_path, mock_gateway, mock_notifier)
        # Same divergence still present on the next tick.
        result = await run_invariant_guard(
            tmp_db_path, mock_gateway, mock_notifier
        )
        assert len(result.escalated) == 1
        assert result.escalated[0].kind == KIND_ORPHAN
        mock_notifier.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_resolved_violation_clears_and_does_not_page(
        self, tmp_db_path: str, mock_gateway, mock_notifier
    ) -> None:
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        await run_invariant_guard(tmp_db_path, mock_gateway, mock_notifier)

        # Resolve it: mark CLOSED so it's no longer open.
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE positions SET status = ? WHERE ticker = ?",
            (PositionStatus.CLOSED.value, "SPY"),
        )
        conn.commit()
        conn.close()

        result = await run_invariant_guard(
            tmp_db_path, mock_gateway, mock_notifier
        )
        assert result.has_violations is False
        assert result.escalated == []
        mock_notifier.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_transient_then_new_violation_not_escalated(
        self, tmp_db_path: str, mock_gateway, mock_notifier
    ) -> None:
        # Tick 1: SPY orphan. Tick 2: SPY resolved, QQQ orphan appears.
        # QQQ is brand new on tick 2 -> must not escalate even though a
        # violation existed on tick 1.
        _insert_position(
            tmp_db_path, "SPY", PositionStatus.STOP_ACTIVE.value
        )
        await run_invariant_guard(tmp_db_path, mock_gateway, mock_notifier)

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE positions SET status = ? WHERE ticker = ?",
            (PositionStatus.CLOSED.value, "SPY"),
        )
        conn.commit()
        conn.close()
        _insert_position(
            tmp_db_path, "QQQ", PositionStatus.STOP_ACTIVE.value
        )

        result = await run_invariant_guard(
            tmp_db_path, mock_gateway, mock_notifier
        )
        assert len(result.violations) == 1
        assert result.violations[0].ticker == "QQQ"
        assert result.escalated == []
        mock_notifier.send.assert_not_called()

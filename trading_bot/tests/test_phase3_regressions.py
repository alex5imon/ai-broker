"""Phase 3 regression tests — live order logic data-layer fixes.

Each bug from trading_bot/docs/self_improve_followups.md gets a deterministic test that
fails on the broken code path and passes after the fix lands. These are the
tests that should fire FIRST if any future change re-introduces one of the
five regressions Phase 3 closes:

B1. trades INSERT in _create_position_record must persist strategy_id.
B2. Failed entries (canceled/expired/rejected/timeout/submit-error) must
    stamp positions with status='ENTRY_FAILED', not 'CLOSED'. ENTRY_FAILED
    is a terminal state added in Phase 2; CLOSED implies a real round-trip.
B3. _close_position's UPDATE on trades must hit the trades.id row, not the
    positions.id row. Pre-fix, the UPDATE used positions.id (called
    `trade_id` everywhere in the code), which silently matched zero or
    wrong rows because trades.id and positions.id are independent
    autoincrements.
B4. recovery._close_db_position must populate strategy_id, derive side
    from the position quantity sign (not hardcode 'SELL'), and stamp the
    ExitReason enum value.
B5. _update_position_field must log rows_affected so future write-back
    failures surface immediately instead of silently no-oping.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.execution.order_manager import (
    EntryDecision,
    OrderManager,
    _ActiveOrder,
)

ET = TZ_EASTERN


# ---------------------------------------------------------------------------
# Test helpers — match patterns in test_order_manager_lifecycle.py
# ---------------------------------------------------------------------------


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _entry(
    *,
    ticker: str = "SPY",
    strategy_id: str = "mean_reversion",
    shares: int = 10,
    price: float = 100.0,
) -> EntryDecision:
    return EntryDecision(
        ticker=ticker,
        exchange="US",
        side="BUY",
        shares=shares,
        limit_price=price,
        stop_price=price * 0.98,
        target_price=price * 1.04,
        hold_type="intraday",
        sector="Information Technology",
        phase=1,
        sentiment_score=0.2,
        signals="test",
        currency="USD",
        strategy_id=strategy_id,
    )


def _alpaca_order(
    *,
    order_id: str = "order-1",
    status: str = "new",
    filled_qty: float = 0.0,
    filled_avg_price: float = 0.0,
    submitted_at: datetime | None = None,
    legs: list[Any] | None = None,
):
    o = MagicMock()
    o.id = order_id
    o.status = MagicMock()
    o.status.value = status
    o.filled_qty = filled_qty
    o.filled_avg_price = filled_avg_price
    o.submitted_at = submitted_at
    o.legs = legs
    return o


# ---------------------------------------------------------------------------
# B1 — strategy_id must be persisted on the entry trades row
# ---------------------------------------------------------------------------


class TestB1_TradesInsertPersistsStrategyId:
    """Live bug: 54/54 trade rows in the GHA-cached DB had strategy_id NULL.
    Root cause: _create_position_record's trades INSERT omits strategy_id
    even though decision.strategy_id is already on hand and is correctly
    written to the positions row 12 lines above.
    """

    def test_strategy_id_written_to_trades_row(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._create_position_record(_entry(strategy_id="overnight_drift"))

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT strategy_id FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "overnight_drift", (
            "trades.strategy_id was NULL on every row in the GHA-cached DB; "
            "fix: extend the trades INSERT in _create_position_record to "
            "bind decision.strategy_id."
        )

    def test_strategy_id_falls_back_to_unknown_when_decision_omits_it(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """Mirrors the positions-side fallback at line 858 so trade rows
        are never NULL for downstream queries."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        decision = _entry()
        decision.strategy_id = None
        om._create_position_record(decision)

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT strategy_id FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "unknown"


# ---------------------------------------------------------------------------
# B2 — failed entries get ENTRY_FAILED, not CLOSED
# ---------------------------------------------------------------------------


class TestB2_FailedEntryGetsEntryFailedStatus:
    """Live bug: 52 phantom-CLOSED rows. Three branches in order_manager
    stamped CLOSED on entries that never filled: canceled-by-broker (line
    244), submit_order raised (line 384), and entry-timeout-with-zero-fill
    (line 445). With ENTRY_FAILED added in Phase 2, each must use it instead.
    """

    @pytest.mark.asyncio
    async def test_submit_order_failure_stamps_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.submit_order = MagicMock(
            side_effect=RuntimeError("insufficient buying power")
        )
        trade_id = await om.place_entry(_entry())
        assert trade_id is None

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value, (
            "submit_order errors used to leave 'CLOSED' rows that polluted "
            "every report. Must now stamp ENTRY_FAILED."
        )

    @pytest.mark.asyncio
    async def test_entry_canceled_by_broker_stamps_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        # Successful submission, then on next status poll Alpaca reports canceled.
        entry_alpaca = _alpaca_order(order_id="entry-1", status="new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        trade_id = await om.place_entry(_entry())
        assert trade_id is not None

        # Now the broker returns 'canceled' on the next status check.
        canceled = _alpaca_order(order_id="entry-1", status="canceled")
        om._gw.client.get_order_by_id = MagicMock(return_value=canceled)
        await om._check_order_statuses()

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = ?", (trade_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value

    @pytest.mark.asyncio
    async def test_entry_timeout_zero_fill_stamps_entry_failed(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        long_ago = datetime.now(tz=ET) - timedelta(hours=1)
        entry_alpaca = _alpaca_order(
            order_id="entry-1", status="new", submitted_at=long_ago,
        )
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        trade_id = await om.place_entry(_entry())

        om._gw.client.get_order_by_id = MagicMock(return_value=entry_alpaca)
        om._gw.client.cancel_order_by_id = MagicMock(return_value=None)
        # Override the entry timeout to a short value so the long_ago
        # submitted_at trips the cancel branch.
        om._config._raw["entry"]["entry_timeout_seconds"] = 60
        await om._check_order_statuses()

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = ?", (trade_id,)
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value


# ---------------------------------------------------------------------------
# B3 — _close_position's exit UPDATE must hit the trades row, not the
# positions row of the same numeric id
# ---------------------------------------------------------------------------


class TestB3_ExitUpdateHitsCorrectTradesRow:
    """Live bug: _close_position used `trade_id` (which is positions.id) as
    the WHERE clause on the trades table. trades.id is an independent
    autoincrement, so the UPDATE matched whatever trades row happened to
    share that integer (or zero rows if the ids had drifted). Pre-fix
    consequence: trade rows never got exit_time / exit_price / exit_reason
    populated even when the exit path fired correctly.
    """

    @pytest.mark.asyncio
    async def test_close_position_writes_exit_data_to_correct_trades_row(
        self, config, tmp_db_path: str, mock_notifier
    ):
        # Pre-seed a row in trades so the row ids drift between tables —
        # this is what reveals the bug. Without drift the broken WHERE
        # clause appears to work by accident.
        conn = sqlite3.connect(tmp_db_path)
        try:
            for _ in range(3):
                conn.execute(
                    "INSERT INTO trades (ticker, exchange, currency, side, "
                    "entry_time, entry_price, quantity, hold_type, phase, "
                    "strategy_id) VALUES "
                    "('NOOP', 'US', 'USD', 'BUY', '2026-01-01T00:00:00', "
                    "1.0, 1, 'intraday', 1, 'mean_reversion')"
                )
            conn.commit()
        finally:
            conn.close()

        om = _make_om(config, tmp_db_path, mock_notifier)
        # place_entry runs the full path: positions insert, trades insert,
        # capture trade_id (positions.id), set alpaca_order_id, build _ActiveOrder.
        entry_alpaca = _alpaca_order(order_id="entry-1", status="new")
        om._gw.client.submit_order = MagicMock(return_value=entry_alpaca)
        trade_id = await om.place_entry(_entry(ticker="SPY"))
        active = om._active_orders[trade_id]
        active.entry_price = 100.0
        active.filled_shares = 10.0

        # Trigger the close path.
        await om._close_position(
            trade_id, active, exit_price=101.5, exit_reason="stop_loss",
        )

        conn = sqlite3.connect(tmp_db_path)
        conn.row_factory = sqlite3.Row
        try:
            # The trades row that should have exit data is the SPY one we
            # just created — find it by ticker, then assert exit_time is set.
            row = conn.execute(
                "SELECT exit_time, exit_price, exit_reason "
                "FROM trades WHERE ticker = 'SPY' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            # And no NOOP row should ever have been touched.
            noop_with_exit = conn.execute(
                "SELECT COUNT(*) FROM trades "
                "WHERE ticker = 'NOOP' AND exit_time IS NOT NULL"
            ).fetchone()[0]
        finally:
            conn.close()

        assert row is not None
        assert row["exit_time"] is not None, (
            "exit UPDATE missed — used positions.id as the WHERE clause "
            "instead of the trades.id captured at insert time."
        )
        assert row["exit_price"] == 101.5
        assert row["exit_reason"] == "stop_loss"
        assert noop_with_exit == 0, (
            "exit UPDATE matched a NOOP trades row — proves the WHERE was "
            "using a positions.id that collided with an unrelated trades.id."
        )


# ---------------------------------------------------------------------------
# B4 — recovery._close_db_position writes complete trades row
# ---------------------------------------------------------------------------


class TestB4_RecoveryCloseDbPositionWritesCompleteRow:
    """Live bug: recovery._close_db_position INSERT INTO trades omitted
    strategy_id and hardcoded side='SELL'. Result: every recovery-driven
    close created a trade row that looked like a short entry (side='SELL'
    is the entry direction in this schema) and had no strategy attribution.
    """

    def _make_recovery(self, config, tmp_db_path: str, mock_notifier):
        from trading_bot.gateway.recovery import StateRecovery

        gw = MagicMock()
        return StateRecovery(gw, tmp_db_path, mock_notifier)

    def _seed_position(
        self,
        tmp_db_path: str,
        *,
        ticker: str = "QQQ",
        qty: int = 10,
        strategy_id: str = "mean_reversion",
    ) -> int:
        conn = sqlite3.connect(tmp_db_path)
        try:
            cur = conn.execute(
                "INSERT INTO positions (ticker, exchange, currency, "
                "quantity, entry_price, entry_time, status, hold_type, "
                "phase, strategy_id) VALUES "
                "(?, 'US', 'USD', ?, 100.0, '2026-04-28T10:00:00', "
                "'POSITION_OPEN', 'intraday', 1, ?)",
                (ticker, qty, strategy_id),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()

    def test_close_db_position_writes_strategy_id_from_position_row(
        self, config, tmp_db_path: str, mock_notifier
    ):
        pos_id = self._seed_position(tmp_db_path, strategy_id="overnight_drift")
        rm = self._make_recovery(config, tmp_db_path, mock_notifier)

        rm._close_db_position(pos_id, "reconciliation_mismatch")

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT strategy_id, side, exit_reason "
                "FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == "overnight_drift", (
            "recovery._close_db_position dropped strategy_id; the trade row "
            "now lives without attribution."
        )
        # Side should reflect the original position direction (long → BUY),
        # not the closing leg. Trades.side semantics in this codebase = entry
        # direction; exit_reason captures the close.
        assert row[1] == "BUY"
        assert row[2] == "reconciliation_mismatch"


# ---------------------------------------------------------------------------
# B5 — _update_position_field observability
# ---------------------------------------------------------------------------


class TestB5_UpdatePositionFieldLogsRowsAffected:
    """User asked for observability inside _update_position_field. The
    investigation already explained every NULL alpaca_order_id (52 from
    submit_order errors → naturally fixed by B2 stamping ENTRY_FAILED, plus
    1 from recovery._create_db_position which has no order id to record).
    This test only pins the new logging contract so a future regression
    where the UPDATE silently no-ops would surface immediately.
    """

    def test_update_logs_rows_affected_at_debug(
        self, config, tmp_db_path: str, mock_notifier, caplog
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry())

        with caplog.at_level(logging.DEBUG, logger="trading_bot.execution.order_manager"):
            om._update_position_field(trade_id, "alpaca_order_id", "ord-xyz")

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "alpaca_order_id" in joined
        assert "rows_affected=1" in joined, (
            "missing rows_affected in log — without it, a silently-failing "
            "UPDATE (e.g. wrong WHERE clause) is invisible."
        )

    def test_update_warns_when_no_rows_match(
        self, config, tmp_db_path: str, mock_notifier, caplog
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        with caplog.at_level(logging.WARNING, logger="trading_bot.execution.order_manager"):
            om._update_position_field(99999, "alpaca_order_id", "ord-xyz")

        joined = "\n".join(rec.getMessage() for rec in caplog.records)
        assert "rows_affected=0" in joined, (
            "0-row UPDATE must log loud — that's the silent-failure mode "
            "the live bot was hiding."
        )


# ---------------------------------------------------------------------------
# B6 — _transition_to_open recovers from a stop-attach response loss
# ---------------------------------------------------------------------------


def _alpaca_open_stop(
    *,
    order_id: str = "stop-rec-1",
    symbol: str = "SPY",
    qty: float = 10.0,
    side: str = "sell",
    order_type: str = "stop",
):
    """Mock an open Alpaca order shaped like get_orders(status=OPEN) returns."""
    o = MagicMock()
    o.id = order_id
    o.symbol = symbol
    o.qty = qty
    o.side = MagicMock()
    o.side.value = side
    o.order_type = MagicMock()
    o.order_type.value = order_type
    return o


class TestB6_TransitionToOpenRecoversFromStopAttachResponseLoss:
    """Live bug observed 2026-04-29 → 2026-05-05: _place_standalone_stop
    occasionally returned None even though the stop order had actually
    been accepted by Alpaca (alpaca-py response parsing raised after the
    order was submitted). _transition_to_open then emergency-flattened —
    which itself failed on the same SDK paths — leaving 7 real positions
    stamped ENTRY_FAILED in the DB while still live at the broker.

    The fix queries Alpaca for an open SELL stop matching the ticker and
    qty before assuming submission failed; if found, the order is adopted
    and the position transitions to STOP_AND_TARGET_ACTIVE normally.
    """

    @pytest.mark.asyncio
    async def test_recovers_when_matching_stop_exists_at_alpaca(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        decision = _entry()
        # Seed an _ActiveOrder as if the entry had just filled.
        trade_id = om._create_position_record(decision)
        active = _ActiveOrder(
            trade_id=trade_id,
            ticker=decision.ticker,
            exchange=decision.exchange,
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.ENTRY_PENDING,
            entry_shares=float(decision.shares),
            filled_shares=float(decision.shares),
            entry_price=decision.limit_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            hold_type=decision.hold_type,
            strategy_id=decision.strategy_id,
        )
        om._active_orders[trade_id] = active

        # Force the submit-response loss path: _place_standalone_stop returns
        # None even though Alpaca actually has the stop. The recovery query
        # finds the matching open SELL stop.
        async def _fail_stop_submit(*args, **kwargs):
            return None
        om._place_standalone_stop = _fail_stop_submit  # type: ignore[assignment]
        recovered = _alpaca_open_stop(
            order_id="recovered-stop",
            symbol=decision.ticker,
            qty=float(decision.shares),
        )
        om._gw.client.get_orders = MagicMock(return_value=[recovered])

        await om._transition_to_open(trade_id, float(decision.shares))

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status, alpaca_stop_order_id "
                "FROM positions WHERE id = ?", (trade_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row[0] == PositionStatus.STOP_AND_TARGET_ACTIVE.value, (
            "Recovery must adopt the live broker stop and proceed to the "
            "normal active-stop status — not stamp ENTRY_FAILED."
        )
        assert row[1] == "recovered-stop"

    @pytest.mark.asyncio
    async def test_falls_through_to_entry_failed_when_no_matching_stop_exists(
        self, config, tmp_db_path: str, mock_notifier
    ):
        om = _make_om(config, tmp_db_path, mock_notifier)
        decision = _entry()
        trade_id = om._create_position_record(decision)
        active = _ActiveOrder(
            trade_id=trade_id,
            ticker=decision.ticker,
            exchange=decision.exchange,
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.ENTRY_PENDING,
            entry_shares=float(decision.shares),
            filled_shares=float(decision.shares),
            entry_price=decision.limit_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            hold_type=decision.hold_type,
            strategy_id=decision.strategy_id,
        )
        om._active_orders[trade_id] = active

        async def _fail_stop_submit(*args, **kwargs):
            return None
        om._place_standalone_stop = _fail_stop_submit  # type: ignore[assignment]
        # No matching stop on the broker — recovery returns nothing.
        om._gw.client.get_orders = MagicMock(return_value=[])
        # Emergency flatten path needs a working market submit_order so the
        # emergency_flatten exception swallow doesn't mask a real bug.
        om._gw.client.submit_order = MagicMock(return_value=_alpaca_order())

        await om._transition_to_open(trade_id, float(decision.shares))

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = ?", (trade_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value, (
            "When no broker-side stop exists, the original ENTRY_FAILED "
            "+ emergency_flatten path must still fire — recovery is for "
            "response loss only, not a real submission failure."
        )

    @pytest.mark.asyncio
    async def test_recovery_ignores_non_matching_orders(
        self, config, tmp_db_path: str, mock_notifier
    ):
        """A stop with a different qty or a BUY-side order must not be
        adopted as the protective stop — that would attach a wrong-sized
        or wrong-direction order to the position."""
        om = _make_om(config, tmp_db_path, mock_notifier)
        decision = _entry(shares=10)
        trade_id = om._create_position_record(decision)
        active = _ActiveOrder(
            trade_id=trade_id,
            ticker=decision.ticker,
            exchange=decision.exchange,
            alpaca_entry_order_id="entry-1",
            status=PositionStatus.ENTRY_PENDING,
            entry_shares=10.0,
            filled_shares=10.0,
            entry_price=decision.limit_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            hold_type=decision.hold_type,
            strategy_id=decision.strategy_id,
        )
        om._active_orders[trade_id] = active

        async def _fail_stop_submit(*args, **kwargs):
            return None
        om._place_standalone_stop = _fail_stop_submit  # type: ignore[assignment]
        wrong_qty = _alpaca_open_stop(
            order_id="wrong-qty", symbol=decision.ticker, qty=5.0,
        )
        wrong_side = _alpaca_open_stop(
            order_id="wrong-side", symbol=decision.ticker, qty=10.0, side="buy",
        )
        wrong_type = _alpaca_open_stop(
            order_id="wrong-type", symbol=decision.ticker, qty=10.0,
            order_type="limit",
        )
        om._gw.client.get_orders = MagicMock(
            return_value=[wrong_qty, wrong_side, wrong_type],
        )
        om._gw.client.submit_order = MagicMock(return_value=_alpaca_order())

        await om._transition_to_open(trade_id, 10.0)

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status, alpaca_stop_order_id "
                "FROM positions WHERE id = ?", (trade_id,),
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == PositionStatus.ENTRY_FAILED.value
        assert row[1] is None or row[1] == "", (
            "No order matches qty+side+type — must NOT adopt any of the "
            "non-matching open orders."
        )

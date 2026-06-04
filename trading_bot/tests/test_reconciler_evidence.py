"""Tests for the reconciler shadow-evidence journal (PR #191 §5 gate evidence).

The journal persists per-day disagreement tallies to ``tick_state`` so the
"flip ``reconciler.drive`` on" (Phase 2b) decision can be made on data rather
than scrollback. Covers round-trip accumulation, per-day separation, retention
pruning, the summary, and that ``run_reconcile`` records evidence each tick.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from trading_bot.constants import PositionStatus, TZ_EASTERN
from trading_bot.execution.reconciler import (
    _EVIDENCE_RETENTION_DAYS,
    DesiredState,
    Disposition,
    ShadowReconcileResult,
    load_shadow_evidence,
    record_shadow_evidence,
    run_reconcile,
    summarize_shadow_evidence,
)

pytestmark = pytest.mark.critical


def _alpaca_position(symbol: str, qty: float):
    p = MagicMock()
    p.symbol = symbol
    p.qty = qty
    return p


def _disp(ticker: str, desired: DesiredState, *, agrees: bool) -> Disposition:
    return Disposition(
        ticker=ticker,
        intent_status=PositionStatus.STOP_ACTIVE.value,
        desired=desired,
        action="",
        agrees_with_db=agrees,
        trade_id=1,
    )


def _result(*disps: Disposition, executed: int = 0) -> ShadowReconcileResult:
    r = ShadowReconcileResult(dispositions=list(disps))
    # executed only needs a length for the tally.
    r.executed = [MagicMock() for _ in range(executed)]
    return r


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


class TestEvidenceJournal:
    def test_empty_when_nothing_recorded(self, tmp_db_path: str) -> None:
        assert load_shadow_evidence(tmp_db_path) == {}

    def test_single_record_round_trip(self, tmp_db_path: str) -> None:
        result = _result(
            _disp("SPY", DesiredState.NEEDS_STOP, agrees=False),
            _disp("QQQ", DesiredState.PROTECTED, agrees=True),
            executed=1,
        )
        record_shadow_evidence(tmp_db_path, result, day_iso="2026-06-04")
        days = load_shadow_evidence(tmp_db_path)
        assert set(days) == {"2026-06-04"}
        t = days["2026-06-04"]
        assert t["ticks"] == 1
        assert t["disagreements"] == 1
        assert t["executed"] == 1
        assert t["max_diff_in_tick"] == 1
        assert t["by_state"] == {"NEEDS_STOP": 1}

    def test_accumulates_across_ticks_same_day(self, tmp_db_path: str) -> None:
        record_shadow_evidence(
            tmp_db_path,
            _result(_disp("SPY", DesiredState.NEEDS_STOP, agrees=False)),
            day_iso="2026-06-04",
        )
        record_shadow_evidence(
            tmp_db_path,
            _result(
                _disp("SPY", DesiredState.NEEDS_STOP, agrees=False),
                _disp("XLE", DesiredState.CLOSED, agrees=False),
                executed=2,
            ),
            day_iso="2026-06-04",
        )
        t = load_shadow_evidence(tmp_db_path)["2026-06-04"]
        assert t["ticks"] == 2
        assert t["disagreements"] == 3
        assert t["executed"] == 2
        assert t["max_diff_in_tick"] == 2  # worst single tick
        assert t["by_state"] == {"NEEDS_STOP": 2, "CLOSED": 1}

    def test_separate_days_tracked_independently(self, tmp_db_path: str) -> None:
        record_shadow_evidence(
            tmp_db_path,
            _result(_disp("SPY", DesiredState.CLOSED, agrees=False)),
            day_iso="2026-06-03",
        )
        record_shadow_evidence(
            tmp_db_path, _result(), day_iso="2026-06-04",
        )
        days = load_shadow_evidence(tmp_db_path)
        assert days["2026-06-03"]["disagreements"] == 1
        assert days["2026-06-04"]["disagreements"] == 0
        assert days["2026-06-04"]["ticks"] == 1

    def test_prunes_to_retention_window(self, tmp_db_path: str) -> None:
        # Record more distinct days than the retention window allows.
        total = _EVIDENCE_RETENTION_DAYS + 5
        for i in range(total):
            record_shadow_evidence(
                tmp_db_path, _result(), day_iso=f"2026-06-{i + 1:02d}",
            )
        days = load_shadow_evidence(tmp_db_path)
        assert len(days) == _EVIDENCE_RETENTION_DAYS
        # Oldest days were pruned; the newest survive.
        assert f"2026-06-{total:02d}" in days
        assert "2026-06-01" not in days

    def test_summary_empty_and_populated(self, tmp_db_path: str) -> None:
        assert "none recorded" in summarize_shadow_evidence({})
        record_shadow_evidence(
            tmp_db_path,
            _result(_disp("SPY", DesiredState.NEEDS_STOP, agrees=False)),
            day_iso="2026-06-04",
        )
        text = summarize_shadow_evidence(load_shadow_evidence(tmp_db_path))
        assert "2026-06-04" in text
        assert "NEEDS_STOP=1" in text


class TestRunReconcileRecordsEvidence:
    @pytest.mark.asyncio
    async def test_tick_records_evidence_under_injected_date(
        self, tmp_db_path: str, mock_gateway
    ) -> None:
        # Orphan -> CLOSED disagreement; injected clock fixes the day.
        _insert_position(
            tmp_db_path, "QQQ", PositionStatus.STOP_ACTIVE.value
        )
        mock_gateway.get_positions.return_value = []
        mock_gateway.get_open_orders.return_value = []
        fixed = datetime(2026, 6, 4, 11, 0, tzinfo=TZ_EASTERN)

        await run_reconcile(tmp_db_path, mock_gateway, drive=False, now=fixed)

        days = load_shadow_evidence(tmp_db_path)
        assert "2026-06-04" in days
        assert days["2026-06-04"]["ticks"] == 1
        assert days["2026-06-04"]["disagreements"] == 1
        assert days["2026-06-04"]["by_state"] == {"CLOSED": 1}

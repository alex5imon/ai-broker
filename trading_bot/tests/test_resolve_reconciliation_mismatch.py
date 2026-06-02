"""Tests for trading_bot.self_improve.resolve_reconciliation_mismatch.

The Alpaca client is never hit: the exit-fill finder and entry-fill confirmer
are injected. The behaviour under test is the per-row decision tree (repair /
void / unresolved), the age gate, idempotency, dry-run safety, and the batched
alert — plus the performance.py exclusion of the new terminal states.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.constants import TZ_EASTERN
from trading_bot.reporting.performance import PerformanceCalculator
from trading_bot.self_improve.alpaca_backfill import ClosedPositionRow, ExitFill
from trading_bot.self_improve.resolve_reconciliation_mismatch import (
    confirm_entry_filled,
    load_stale_rows,
    resolve,
)

# A fixed "now" so age math is deterministic.
NOW = datetime(2026, 6, 1, 21, 30, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _insert_mismatch(
    conn,
    *,
    ticker: str = "SPY",
    strategy_id: str | None = "overnight_drift",
    entry_dt: datetime,
    exit_dt: datetime | None = None,
    quantity: float = 10.0,
    entry_price: float = 100.0,
    with_position: bool = True,
    entry_oid: str | None = "alp-entry-1",
) -> int:
    """Insert a reconciliation_mismatch trades row (NULL exit) and, optionally,
    its matching positions row. Returns the trade id."""
    # Mirror production: entry/exit times are stored ET-aware ISO strings.
    entry_iso = entry_dt.astimezone(TZ_EASTERN).isoformat()
    exit_iso = exit_dt.astimezone(TZ_EASTERN).isoformat() if exit_dt is not None else None
    conn.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side, entry_time, entry_price,
            quantity, exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, pnl_usd, hold_type, phase, strategy_id, notes
        ) VALUES (?, 'NYSE', 'USD', 'BUY', ?, ?, ?, ?, NULL,
                  'reconciliation_mismatch', NULL, NULL, NULL,
                  'swing', 3, ?, 'Auto-closed by state recovery')
        """,
        (ticker, entry_iso, entry_price, quantity, exit_iso, strategy_id),
    )
    trade_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    if with_position:
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price, entry_time,
                status, hold_type, phase, alpaca_order_id, strategy_id
            ) VALUES (?, 'NYSE', 'USD', ?, ?, ?, 'CLOSED', 'swing', 3, ?, ?)
            """,
            (ticker, quantity, entry_price, entry_iso, entry_oid, strategy_id),
        )
    conn.commit()
    return trade_id


def _finder_returns(fill: ExitFill | None):
    async def _f(client, position: ClosedPositionRow):
        return fill
    return _f


async def _confirm_true(client, order_id: str) -> bool:
    return True


async def _confirm_false(client, order_id: str) -> bool:
    return False


def _row(conn, trade_id: int) -> dict:
    cur = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
    cols = [c[0] for c in cur.description]
    return dict(zip(cols, cur.fetchone()))


# ---------------------------------------------------------------------------
# load_stale_rows
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_stale_rows_selects_only_null_exit_mismatch(tmp_db):
    old = NOW - timedelta(days=30)
    keep = _insert_mismatch(tmp_db, ticker="XLF", entry_dt=old, exit_dt=old)
    # A complete (already-resolved) row must NOT be selected.
    tmp_db.execute(
        """
        INSERT INTO trades (ticker, exchange, currency, side, entry_time,
            entry_price, quantity, exit_time, exit_price, exit_reason,
            pnl_usd, hold_type, phase, strategy_id)
        VALUES ('QQQ','NYSE','USD','BUY', ?, 100, 5, ?, 99, 'stop_loss',
                -5, 'swing', 3, 'overnight_drift')
        """,
        (old.isoformat(), old.isoformat()),
    )
    tmp_db.commit()

    rows, skipped = load_stale_rows(tmp_db, now=NOW)
    assert {r.trade_id for r in rows} == {keep}
    assert skipped == 0


@pytest.mark.unit
def test_load_stale_rows_age_gate_skips_young(tmp_db):
    young = _insert_mismatch(tmp_db, ticker="XLF", entry_dt=NOW, exit_dt=NOW)  # same day
    old = _insert_mismatch(tmp_db, ticker="XLE", entry_dt=NOW - timedelta(days=5),
                           exit_dt=NOW - timedelta(days=5))
    rows, skipped = load_stale_rows(tmp_db, now=NOW, min_age_days=2)
    assert {r.trade_id for r in rows} == {old}
    assert skipped == 1
    assert young not in {r.trade_id for r in rows}


@pytest.mark.unit
def test_load_stale_rows_joins_position_order_ids(tmp_db):
    old = NOW - timedelta(days=10)
    _insert_mismatch(tmp_db, ticker="XLF", entry_dt=old, exit_dt=old, entry_oid="alp-xyz")
    rows, _ = load_stale_rows(tmp_db, now=NOW)
    assert rows[0].alpaca_order_id == "alp-xyz"


@pytest.mark.unit
def test_load_stale_rows_handles_missing_position(tmp_db):
    """Attribution-less rows with no surviving position must still load (the
    Root-Cause-A fix: trades-driven, not positions-driven)."""
    old = NOW - timedelta(days=10)
    tid = _insert_mismatch(tmp_db, ticker="XLB", strategy_id="unknown",
                           entry_dt=old, exit_dt=old, with_position=False)
    rows, _ = load_stale_rows(tmp_db, now=NOW)
    assert {r.trade_id for r in rows} == {tid}
    assert rows[0].alpaca_order_id is None


# ---------------------------------------------------------------------------
# resolve — decision tree
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_repairs_when_fill_found(tmp_db):
    old = NOW - timedelta(days=10)
    tid = _insert_mismatch(tmp_db, ticker="XLF", entry_dt=old, exit_dt=old,
                           entry_price=100.0, quantity=10.0)
    fill = ExitFill(order_id="sell-1", filled_at=old + timedelta(hours=2),
                    filled_avg_price=101.0, filled_qty=10.0)

    report = await resolve(tmp_db, client=None, now=NOW,
                           fill_finder=_finder_returns(fill),
                           entry_confirmer=_confirm_false)

    assert report.repaired == 1 and report.voided == 0 and report.unresolved == 0
    row = _row(tmp_db, tid)
    assert row["exit_price"] == 101.0
    assert row["exit_reason"] != "reconciliation_mismatch"
    assert row["pnl_usd"] == pytest.approx(10.0)  # (101-100)*10


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_voids_when_no_fill_and_entry_unconfirmed(tmp_db):
    old = NOW - timedelta(days=30)
    tid = _insert_mismatch(tmp_db, ticker="XLK", entry_dt=old, exit_dt=old,
                           entry_price=200.0, quantity=3.0)

    report = await resolve(tmp_db, client=None, now=NOW,
                           fill_finder=_finder_returns(None),
                           entry_confirmer=_confirm_false)

    assert report.voided == 1 and report.repaired == 0 and report.unresolved == 0
    row = _row(tmp_db, tid)
    assert row["exit_reason"] == "void_no_fill"
    assert row["pnl_usd"] == 0
    assert row["exit_price"] == 200.0  # entry_price -> flat scratch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_flags_unresolved_when_entry_confirmed_filled(tmp_db):
    old = NOW - timedelta(days=4)
    tid = _insert_mismatch(tmp_db, ticker="MSFT", entry_dt=old, exit_dt=old)
    alerts: list[tuple[str, str]] = []

    report = await resolve(tmp_db, client=None, now=NOW,
                           fill_finder=_finder_returns(None),
                           entry_confirmer=_confirm_true,
                           alert=lambda t, m: alerts.append((t, m)))

    assert report.unresolved == 1 and report.voided == 0
    row = _row(tmp_db, tid)
    assert row["exit_reason"] == "unresolved_exit"
    assert row["exit_price"] is None  # never fabricated
    assert len(alerts) == 1  # batched, fired once


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_never_voids_confirmed_filled_row(tmp_db):
    """Safety invariant: a row whose entry is provably filled must never be
    zeroed — only flagged."""
    old = NOW - timedelta(days=40)
    tid = _insert_mismatch(tmp_db, ticker="XLV", entry_dt=old, exit_dt=old)
    await resolve(tmp_db, client=None, now=NOW,
                  fill_finder=_finder_returns(None), entry_confirmer=_confirm_true)
    assert _row(tmp_db, tid)["exit_reason"] == "unresolved_exit"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_alert_batched_once_for_many(tmp_db):
    old = NOW - timedelta(days=10)
    for tk in ("AAA", "BBB", "CCC"):
        _insert_mismatch(tmp_db, ticker=tk, entry_dt=old, exit_dt=old)
    alerts: list[tuple[str, str]] = []
    report = await resolve(tmp_db, client=None, now=NOW,
                           fill_finder=_finder_returns(None),
                           entry_confirmer=_confirm_true,
                           alert=lambda t, m: alerts.append((t, m)))
    assert report.unresolved == 3
    assert len(alerts) == 1
    assert "3" in alerts[0][1]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_dry_run_writes_nothing(tmp_db):
    old = NOW - timedelta(days=30)
    tid = _insert_mismatch(tmp_db, ticker="XLK", entry_dt=old, exit_dt=old)
    report = await resolve(tmp_db, client=None, now=NOW, dry_run=True,
                           fill_finder=_finder_returns(None),
                           entry_confirmer=_confirm_false)
    assert report.voided == 1  # counted
    assert _row(tmp_db, tid)["exit_reason"] == "reconciliation_mismatch"  # untouched


@pytest.mark.unit
@pytest.mark.asyncio
async def test_resolve_is_idempotent(tmp_db):
    old = NOW - timedelta(days=30)
    _insert_mismatch(tmp_db, ticker="XLK", entry_dt=old, exit_dt=old)
    first = await resolve(tmp_db, client=None, now=NOW,
                          fill_finder=_finder_returns(None), entry_confirmer=_confirm_false)
    second = await resolve(tmp_db, client=None, now=NOW,
                           fill_finder=_finder_returns(None), entry_confirmer=_confirm_false)
    assert first.voided == 1
    assert second.candidates == 0  # nothing left to resolve


# ---------------------------------------------------------------------------
# confirm_entry_filled
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_entry_filled_empty_id_is_false():
    assert await confirm_entry_filled(client=None, order_id="") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_entry_filled_true_for_filled():
    class _Status:
        value = "filled"

    class _Order:
        status = _Status()

    class _Client:
        def get_order_by_id(self, oid):
            return _Order()

    assert await confirm_entry_filled(_Client(), "alp-1") is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_entry_filled_false_on_exception():
    class _Client:
        def get_order_by_id(self, oid):
            raise RuntimeError("404")

    assert await confirm_entry_filled(_Client(), "alp-1") is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_confirm_entry_filled_false_for_partial():
    class _Status:
        value = "partially_filled"

    class _Order:
        status = _Status()

    class _Client:
        def get_order_by_id(self, oid):
            return _Order()

    assert await confirm_entry_filled(_Client(), "alp-1") is False


# ---------------------------------------------------------------------------
# performance.py exclusion of terminal markers
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_daily_metrics_excludes_void_and_unresolved(tmp_db_path):
    import sqlite3

    conn = sqlite3.connect(tmp_db_path)
    et_day = "2026-04-30"
    # one real trade, one void, one unresolved — only the real one counts.
    conn.execute(
        """INSERT INTO trades (ticker, exchange, currency, side, entry_time,
           entry_price, quantity, exit_time, exit_price, exit_reason,
           gross_pnl, pnl_usd, hold_type, phase, strategy_id)
           VALUES ('SPY','NYSE','USD','BUY','2026-04-30T09:30:00-04:00',100,10,
                   '2026-04-30T10:00:00-04:00',101,'take_profit',10,10,'swing',3,'mr')"""
    )
    conn.execute(
        """INSERT INTO trades (ticker, exchange, currency, side, entry_time,
           entry_price, quantity, exit_time, exit_price, exit_reason, pnl_usd,
           hold_type, phase, strategy_id)
           VALUES ('XLF','NYSE','USD','BUY','2026-04-30T09:30:00-04:00',50,10,
                   '2026-04-30T10:00:00-04:00',50,'void_no_fill',0,'swing',3,'od')"""
    )
    conn.execute(
        """INSERT INTO trades (ticker, exchange, currency, side, entry_time,
           entry_price, quantity, exit_time, exit_price, exit_reason, pnl_usd,
           hold_type, phase, strategy_id)
           VALUES ('XLE','NYSE','USD','BUY','2026-04-30T09:30:00-04:00',60,10,
                   '2026-04-30T10:00:00-04:00',NULL,'unresolved_exit',NULL,'swing',3,'od')"""
    )
    conn.commit()
    conn.close()

    metrics = PerformanceCalculator(tmp_db_path).calculate_daily_metrics(et_day)
    assert metrics["total_trades"] == 1
    assert metrics["wins"] == 1
    assert metrics["net_pnl_usd"] == pytest.approx(10.0)

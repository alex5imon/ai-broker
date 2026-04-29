"""Tests for trading_bot.self_improve.alpaca_backfill.

The Alpaca client is a stub — we never hit the network. The interesting
behaviour to test is candidate selection (idempotency, status filter,
strategy attribution), exit_reason inference, and the trades-row insert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.self_improve.alpaca_backfill import (
    BACKFILL_MARKER_PREFIX,
    ClosedPositionRow,
    ExitFill,
    _infer_exit_reason,
    backfill,
    insert_backfilled_trade,
    load_candidates,
)


def _insert_position(
    conn,
    *,
    position_id: int,
    ticker: str = "SPY",
    strategy_id: str = "overnight_drift",
    status: str = "CLOSED",
    quantity: int = 10,
    entry_price: float = 100.0,
    entry_time: datetime,
    alpaca_order_id: str = "alp-entry-001",
    alpaca_stop_order_id: str | None = None,
    alpaca_target_order_id: str | None = None,
    alpaca_trail_order_id: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO positions (
            id, ticker, exchange, currency, quantity,
            entry_price, entry_time, status, hold_type, phase,
            alpaca_order_id, alpaca_stop_order_id,
            alpaca_target_order_id, alpaca_trail_order_id,
            strategy_id
        ) VALUES (?, ?, 'NYSE', 'USD', ?, ?, ?, ?, 'swing', 1, ?, ?, ?, ?, ?)
        """,
        (
            position_id, ticker, quantity, entry_price,
            entry_time.strftime("%Y-%m-%dT%H:%M:%S%z") or entry_time.isoformat(),
            status,
            alpaca_order_id, alpaca_stop_order_id,
            alpaca_target_order_id, alpaca_trail_order_id,
            strategy_id,
        ),
    )


def _make_position(
    position_id: int = 1,
    ticker: str = "SPY",
    *,
    stop_id: str | None = None,
    target_id: str | None = None,
    trail_id: str | None = None,
) -> ClosedPositionRow:
    return ClosedPositionRow(
        position_id=position_id,
        ticker=ticker,
        exchange="NYSE",
        currency="USD",
        strategy_id="overnight_drift",
        quantity=10,
        entry_price=100.0,
        entry_time=datetime(2026, 4, 1, 15, 45, tzinfo=timezone.utc),
        hold_type="swing",
        phase=1,
        alpaca_order_id="alp-entry",
        alpaca_stop_order_id=stop_id,
        alpaca_target_order_id=target_id,
        alpaca_trail_order_id=trail_id,
    )


# ---------------------------------------------------------------------------
# load_candidates
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_load_candidates_returns_only_closed_with_strategy(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=1, status="CLOSED", strategy_id="mean_reversion",
                     entry_time=now)
    _insert_position(tmp_db, position_id=2, status="POSITION_OPEN",
                     strategy_id="overnight_drift", entry_time=now)
    _insert_position(tmp_db, position_id=3, status="CLOSED", strategy_id="unknown",
                     entry_time=now)
    tmp_db.commit()

    candidates = load_candidates(tmp_db)
    ids = {c.position_id for c in candidates}
    assert ids == {1}


@pytest.mark.unit
def test_load_candidates_skips_already_backfilled(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=42, entry_time=now)
    # Pre-existing trades row marking this position as backfilled
    tmp_db.execute(
        """
        INSERT INTO trades (ticker, exchange, currency, side, entry_time, entry_price,
                            quantity, hold_type, phase, strategy_id, notes)
        VALUES ('SPY','NYSE','USD','long', ?, 100, 10, 'swing', 1, 'overnight_drift', ?)
        """,
        (now.strftime("%Y-%m-%d %H:%M:%S"), f"{BACKFILL_MARKER_PREFIX}42"),
    )
    tmp_db.commit()

    assert load_candidates(tmp_db) == []


# ---------------------------------------------------------------------------
# _infer_exit_reason
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_infer_exit_reason_matches_stop():
    p = _make_position(stop_id="alp-stop", target_id="alp-target", trail_id="alp-trail")
    assert _infer_exit_reason("alp-stop", p) == "stop_loss"
    assert _infer_exit_reason("alp-target", p) == "take_profit"
    assert _infer_exit_reason("alp-trail", p) == "trailing_stop"


@pytest.mark.unit
def test_infer_exit_reason_unknown_falls_back_to_manual():
    p = _make_position(stop_id="alp-stop")
    assert _infer_exit_reason("alp-unrelated", p) == "manual"


@pytest.mark.unit
def test_infer_exit_reason_handles_missing_bracket_ids():
    p = _make_position()  # no stop/target/trail
    assert _infer_exit_reason("alp-anything", p) == "manual"


# ---------------------------------------------------------------------------
# insert_backfilled_trade
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_insert_writes_complete_row(tmp_db):
    p = _make_position(stop_id="alp-stop")
    fill = ExitFill(
        order_id="alp-stop",
        filled_at=datetime(2026, 4, 2, 9, 31, tzinfo=timezone.utc),
        filled_avg_price=98.0,
        filled_qty=10.0,
    )
    insert_backfilled_trade(tmp_db, p, fill)
    tmp_db.commit()

    row = tmp_db.execute(
        "SELECT ticker, strategy_id, exit_reason, gross_pnl, net_pnl, notes FROM trades"
    ).fetchone()
    assert row[0] == "SPY"
    assert row[1] == "overnight_drift"
    assert row[2] == "stop_loss"
    assert row[3] == pytest.approx((98.0 - 100.0) * 10)
    assert row[4] == pytest.approx((98.0 - 100.0) * 10)
    assert row[5] == f"{BACKFILL_MARKER_PREFIX}1"


# ---------------------------------------------------------------------------
# backfill (orchestrator with stubbed fill finder)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_inserts_one_row_per_paired_position(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=1, ticker="SPY", entry_time=now,
                     alpaca_target_order_id="tgt-1")
    _insert_position(tmp_db, position_id=2, ticker="QQQ", entry_time=now,
                     alpaca_stop_order_id="stp-2")
    tmp_db.commit()

    async def stub_finder(client, position):
        if position.ticker == "SPY":
            return ExitFill(
                order_id="tgt-1", filled_at=now + timedelta(hours=1),
                filled_avg_price=101.0, filled_qty=10,
            )
        return ExitFill(
            order_id="stp-2", filled_at=now + timedelta(hours=1),
            filled_avg_price=99.0, filled_qty=10,
        )

    report = await backfill(tmp_db, client=None, fill_finder=stub_finder)
    assert report.inserted == 2
    assert report.no_exit_found == 0

    rows = [
        tuple(r) for r in tmp_db.execute(
            "SELECT ticker, exit_reason, gross_pnl FROM trades ORDER BY ticker"
        ).fetchall()
    ]
    assert rows[0] == ("QQQ", "stop_loss", pytest.approx(-10.0))
    assert rows[1] == ("SPY", "take_profit", pytest.approx(10.0))


@pytest.mark.asyncio
async def test_backfill_dry_run_writes_nothing(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=1, entry_time=now)
    tmp_db.commit()

    async def stub_finder(client, position):
        return ExitFill(order_id="x", filled_at=now + timedelta(hours=1),
                        filled_avg_price=101.0, filled_qty=10)

    report = await backfill(tmp_db, client=None, fill_finder=stub_finder, dry_run=True)
    assert report.inserted == 0
    assert report.dry_run is True
    assert tmp_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_backfill_handles_no_exit_found(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=1, entry_time=now)
    tmp_db.commit()

    async def stub_finder(client, position):
        return None

    report = await backfill(tmp_db, client=None, fill_finder=stub_finder)
    assert report.inserted == 0
    assert report.no_exit_found == 1


@pytest.mark.asyncio
async def test_backfill_is_idempotent(tmp_db):
    """Re-running backfill on a DB that already has the row should be a no-op."""
    now = datetime.now(timezone.utc)
    _insert_position(tmp_db, position_id=1, entry_time=now)
    tmp_db.commit()

    async def stub_finder(client, position):
        return ExitFill(order_id="x", filled_at=now + timedelta(hours=1),
                        filled_avg_price=101.0, filled_qty=10)

    first = await backfill(tmp_db, client=None, fill_finder=stub_finder)
    assert first.inserted == 1
    second = await backfill(tmp_db, client=None, fill_finder=stub_finder)
    assert second.inserted == 0
    assert second.candidates_found == 0
    assert tmp_db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1

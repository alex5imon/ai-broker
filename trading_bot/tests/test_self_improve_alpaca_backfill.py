"""Tests for trading_bot.self_improve.alpaca_backfill.

The Alpaca client is a stub — we never hit the network. The interesting
behaviour to test is candidate selection (idempotency, status filter,
strategy attribution), exit_reason inference, and the trades-row insert.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.constants import TZ_EASTERN
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
    quantity: float = 10,
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
    # entry_time is ET-aware to mirror production: load_candidates()
    # parses positions.entry_time (stored as ET-aware ISO by the live
    # writer) and preserves the original tz. Tests that previously
    # constructed this as UTC were leaning on the now-removed
    # strftime() format coincidence — see ai-broker#40.
    return ClosedPositionRow(
        position_id=position_id,
        ticker=ticker,
        exchange="NYSE",
        currency="USD",
        strategy_id="overnight_drift",
        quantity=10.0,
        entry_price=100.0,
        entry_time=datetime(2026, 4, 1, 15, 45, tzinfo=TZ_EASTERN),
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


@pytest.mark.unit
def test_backfill_updates_existing_reconciliation_mismatch_row(tmp_db):
    """Live bug: StateRecovery._close_db_position writes a trades row
    with NULL exit_price and exit_reason='reconciliation_mismatch'.
    Pre-fix backfill INSERTed a separate row, leaving daily_summaries
    blind to the actual P&L (it can't tell which of the two duplicates
    is the truth) and bloating the table by one row per close per night.

    The repair must UPDATE the existing reconciliation_mismatch row,
    not insert.
    """
    p = _make_position(stop_id="alp-stop")
    # Seed the reconciliation_mismatch row that StateRecovery writes.
    # entry_time/exit_time are ET-aware ISO matching the live writer
    # (repository._now_eastern_iso) — the format the post-#40 backfill
    # matches against.
    tmp_db.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side,
            entry_time, entry_price, quantity,
            exit_time, exit_reason,
            hold_type, phase, strategy_id, notes
        ) VALUES ('SPY', 'NYSE', 'USD', 'BUY',
                  '2026-04-01T15:45:00-04:00', 100.0, 10,
                  '2026-04-02T05:00:00-04:00', 'reconciliation_mismatch',
                  'swing', 1, 'overnight_drift', 'placeholder')
        """,
    )
    tmp_db.commit()

    fill = ExitFill(
        order_id="alp-stop",
        filled_at=datetime(2026, 4, 2, 9, 31, tzinfo=timezone.utc),
        filled_avg_price=98.0,
        filled_qty=10.0,
    )
    insert_backfilled_trade(tmp_db, p, fill)
    tmp_db.commit()

    rows = tmp_db.execute(
        "SELECT id, exit_reason, exit_price, pnl_usd, notes FROM trades "
        "WHERE ticker='SPY' ORDER BY id"
    ).fetchall()
    assert len(rows) == 1, (
        "backfill must UPDATE the existing reconciliation_mismatch row, "
        "not insert a duplicate."
    )
    row = rows[0]
    assert row[1] == "stop_loss", "exit_reason rewritten with the real one"
    assert row[2] == pytest.approx(98.0)
    assert row[3] == pytest.approx((98.0 - 100.0) * 10)
    assert row[4] == f"{BACKFILL_MARKER_PREFIX}1", (
        "notes carries the backfill marker so load_candidates skips next time"
    )


@pytest.mark.unit
def test_backfill_update_path_preserves_fractional_pnl(tmp_db):
    """The UPDATE branch of insert_backfilled_trade computes pnl from
    ``ClosedPositionRow.quantity`` — same call site as the INSERT branch.
    Mirrors the 2026-05-12 live failure where a 0.3927-share XLK position
    landed in the reconciliation_mismatch UPDATE path: with quantity
    truncated to 0, the UPDATE wrote pnl_usd=0.0 instead of the real
    ~-$1.29.
    """
    p = ClosedPositionRow(
        position_id=1,
        ticker="XLK",
        exchange="NYSE",
        currency="USD",
        strategy_id="overnight_drift",
        quantity=0.3927,
        entry_price=177.438,
        entry_time=datetime(2026, 5, 11, 15, 45, tzinfo=TZ_EASTERN),
        hold_type="swing",
        phase=1,
        alpaca_order_id="alp-entry-xlk",
        alpaca_stop_order_id=None,
        alpaca_target_order_id=None,
        alpaca_trail_order_id=None,
    )
    tmp_db.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side,
            entry_time, entry_price, quantity,
            exit_time, exit_reason,
            hold_type, phase, strategy_id, notes
        ) VALUES ('XLK', 'NYSE', 'USD', 'BUY',
                  '2026-05-11T15:45:00-04:00', 177.438, 0.3927,
                  '2026-05-12T09:40:34-04:00', 'reconciliation_mismatch',
                  'swing', 1, 'overnight_drift', 'placeholder')
        """,
    )
    tmp_db.commit()

    insert_backfilled_trade(tmp_db, p, ExitFill(
        order_id="alp-exit-xlk",
        filled_at=datetime(2026, 5, 12, 13, 30, tzinfo=timezone.utc),
        filled_avg_price=174.154,
        filled_qty=0.3927,
    ))
    tmp_db.commit()

    rows = tmp_db.execute(
        "SELECT exit_reason, exit_price, gross_pnl, pnl_usd FROM trades "
        "WHERE ticker='XLK'"
    ).fetchall()
    assert len(rows) == 1, "UPDATE in place — no duplicate row inserted"
    expected_pnl = (174.154 - 177.438) * 0.3927
    assert rows[0][0] == "manual"
    assert rows[0][1] == pytest.approx(174.154)
    assert rows[0][2] == pytest.approx(expected_pnl)
    assert rows[0][3] == pytest.approx(expected_pnl)


@pytest.mark.unit
def test_backfill_inserts_when_no_reconciliation_row_present(tmp_db):
    """Older closed positions that predate the recovery-write path don't
    have a reconciliation_mismatch row to update — INSERT remains the
    correct fallback."""
    p = _make_position(stop_id="alp-stop")
    fill = ExitFill(
        order_id="alp-stop",
        filled_at=datetime(2026, 4, 2, 9, 31, tzinfo=timezone.utc),
        filled_avg_price=98.0,
        filled_qty=10.0,
    )
    insert_backfilled_trade(tmp_db, p, fill)
    tmp_db.commit()

    rows = tmp_db.execute(
        "SELECT exit_price, notes FROM trades WHERE ticker='SPY'"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == pytest.approx(98.0)
    assert rows[0][1] == f"{BACKFILL_MARKER_PREFIX}1"


@pytest.mark.asyncio
async def test_backfill_preserves_fractional_quantity(tmp_db):
    """Live bug 2026-05-12: ``ClosedPositionRow.quantity`` was typed
    ``int`` and ``load_candidates`` cast ``int(row[5])``, truncating
    fractional shares to 0/1. A 0.3927-share XLK position recorded
    pnl=$0.00 instead of the real -$1.29 — masking realized losses on
    every sub-1-share fractional close.

    The fix preserves float precision through the candidate row and into
    the ``gross_pnl = (exit - entry) * quantity`` calculation.
    """
    entry_time = datetime(2026, 5, 12, 15, 45, tzinfo=TZ_EASTERN)
    tmp_db.execute(
        """
        INSERT INTO positions (
            id, ticker, exchange, currency, quantity,
            entry_price, entry_time, status, hold_type, phase,
            alpaca_order_id, strategy_id
        ) VALUES (1, 'XLK', 'NYSE', 'USD', 0.3927,
                  177.438, ?, 'CLOSED', 'swing', 1,
                  'alp-entry-xlk', 'overnight_drift')
        """,
        (entry_time.isoformat(),),
    )
    tmp_db.commit()

    candidates = load_candidates(tmp_db)
    assert len(candidates) == 1
    assert candidates[0].quantity == pytest.approx(0.3927), (
        "fractional quantity must round-trip from the positions row — "
        "int() truncation drops sub-share precision"
    )

    async def stub_finder(client, position):
        return ExitFill(
            order_id="alp-exit-xlk",
            filled_at=entry_time + timedelta(hours=18),
            filled_avg_price=174.154,
            filled_qty=0.3927,
        )

    report = await backfill(tmp_db, client=None, fill_finder=stub_finder)
    assert report.inserted == 1

    row = tmp_db.execute(
        "SELECT quantity, gross_pnl, pnl_usd FROM trades WHERE ticker='XLK'"
    ).fetchone()
    expected_pnl = (174.154 - 177.438) * 0.3927
    assert row[0] == pytest.approx(0.3927)
    assert row[1] == pytest.approx(expected_pnl)
    assert row[2] == pytest.approx(expected_pnl)


@pytest.mark.unit
def test_backfill_only_updates_matching_entry_time(tmp_db):
    """Two closes on the same ticker on different days must not collide:
    the UPDATE keys on entry_time and only modifies the matching row."""
    p1 = _make_position(
        position_id=1, stop_id="alp-stop-1",
    )

    # Seed two reconciliation_mismatch rows, one per day. Format matches
    # the live writer (ET-aware ISO).
    for entry_time, ep in [
        ("2026-04-01T15:45:00-04:00", 100.0),
        ("2026-05-05T15:45:00-04:00", 110.0),
    ]:
        tmp_db.execute(
            """
            INSERT INTO trades (
                ticker, exchange, currency, side,
                entry_time, entry_price, quantity,
                exit_time, exit_reason,
                hold_type, phase, strategy_id
            ) VALUES ('SPY', 'NYSE', 'USD', 'BUY',
                      ?, ?, 10,
                      '2026-05-06T05:00:00-04:00', 'reconciliation_mismatch',
                      'swing', 1, 'overnight_drift')
            """,
            (entry_time, ep),
        )
    tmp_db.commit()

    insert_backfilled_trade(tmp_db, p1, ExitFill(
        order_id="alp-stop-1",
        filled_at=datetime(2026, 4, 2, 9, 31, tzinfo=timezone.utc),
        filled_avg_price=98.0, filled_qty=10.0,
    ))
    tmp_db.commit()

    rows = tmp_db.execute(
        "SELECT entry_time, exit_price, pnl_usd FROM trades "
        "WHERE ticker='SPY' ORDER BY entry_time"
    ).fetchall()
    assert len(rows) == 2
    # The April row got its exit price filled in.
    assert rows[0][1] == pytest.approx(98.0)
    # The May row stays untouched.
    assert rows[1][1] is None, (
        "the day-2 reconciliation_mismatch row must not be modified by "
        "the day-1 backfill — entry_time disambiguates them."
    )


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

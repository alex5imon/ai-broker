"""Tests for trading_bot.self_improve.recompute_daily_summaries.

The recompute step closes the gap between the live wind-down
(daily_summary written at 16:10 ET with NULL pnl_usd in trades) and the
21:30 UTC backfill (which populates pnl_usd). Without it, daily_summaries
stays at wins=0/losses=0/net=0 even on busy trading days.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest

from trading_bot.self_improve.recompute_daily_summaries import (
    _dates_with_trades,
    recompute_for_dates,
)


@contextmanager
def _conn(db_path: str):
    c = sqlite3.connect(db_path)
    try:
        yield c
    finally:
        c.close()


def _seed_summary(
    conn: sqlite3.Connection, *, date: str, equity: float = 100000.0,
):
    """Seed a stale-zeros daily_summaries row mimicking the live writer."""
    conn.execute(
        """INSERT INTO daily_summaries (
            date, total_trades, wins, losses,
            gross_pnl_usd, commissions_usd, net_pnl_usd,
            account_equity_usd, phase, us_trades
        ) VALUES (?, 0, 0, 0, 0.0, 0.0, 0.0, ?, 3, 0)""",
        (date, equity),
    )


def _seed_trade(
    conn: sqlite3.Connection, *,
    ticker: str, exit_date: str, gross: float, pnl: float,
):
    conn.execute(
        """INSERT INTO trades (
            ticker, exchange, currency, side,
            entry_time, entry_price, quantity,
            exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, pnl_usd,
            hold_type, phase, strategy_id
        ) VALUES (?, 'US', 'USD', 'BUY',
                  ?, 100.0, 10,
                  ?, ?, 'stop_loss',
                  ?, ?, ?,
                  'swing', 3, 'overnight_drift')""",
        (
            ticker,
            f"{exit_date}T09:00:00",
            f"{exit_date}T15:00:00",
            100.0 + (pnl / 10),  # plausible exit price
            gross, gross, pnl,
        ),
    )


@pytest.mark.unit
def test_recompute_overwrites_stale_zeros_with_real_metrics(tmp_db_path):
    """Pre-condition: daily_summaries written by wind-down with NULL
    pnl_usd in trades → all zeros. After backfill populates pnl_usd, the
    recompute fills in the real wins/losses/net."""
    with _conn(tmp_db_path) as conn:
        _seed_summary(conn, date="2026-04-30")
        _seed_trade(conn, ticker="SPY", exit_date="2026-04-30",
                    gross=12.0, pnl=12.0)
        _seed_trade(conn, ticker="QQQ", exit_date="2026-04-30",
                    gross=-5.0, pnl=-5.0)
        _seed_trade(conn, ticker="XLK", exit_date="2026-04-30",
                    gross=8.0, pnl=8.0)
        conn.commit()

        written = recompute_for_dates(
            conn, ["2026-04-30"], phase=3, dry_run=False,
        )
        assert written == 1

        row = conn.execute(
            "SELECT total_trades, wins, losses, net_pnl_usd, notes "
            "FROM daily_summaries WHERE date = '2026-04-30'"
        ).fetchone()
    assert row[0] == 3
    assert row[1] == 2  # wins
    assert row[2] == 1  # losses
    assert row[3] == pytest.approx(15.0)  # 12 - 5 + 8
    assert "recomputed:post_backfill" in (row[4] or "")


@pytest.mark.unit
def test_recompute_skips_dates_with_no_existing_summary(tmp_db_path):
    """Without an existing daily_summaries row we have no
    account_equity_usd to write — the column is NOT NULL. Skip rather
    than make up a value."""
    with _conn(tmp_db_path) as conn:
        _seed_trade(conn, ticker="SPY", exit_date="2026-04-29",
                    gross=10.0, pnl=10.0)
        conn.commit()

        written = recompute_for_dates(
            conn, ["2026-04-29"], phase=3, dry_run=False,
        )
        assert written == 0

        row = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries WHERE date='2026-04-29'"
        ).fetchone()
    assert row[0] == 0


@pytest.mark.unit
def test_recompute_dry_run_makes_no_writes(tmp_db_path):
    with _conn(tmp_db_path) as conn:
        _seed_summary(conn, date="2026-05-01")
        _seed_trade(conn, ticker="SPY", exit_date="2026-05-01",
                    gross=20.0, pnl=20.0)
        conn.commit()

        written = recompute_for_dates(
            conn, ["2026-05-01"], phase=3, dry_run=True,
        )
        assert written == 1

        row = conn.execute(
            "SELECT wins, net_pnl_usd FROM daily_summaries "
            "WHERE date='2026-05-01'"
        ).fetchone()
    assert row[0] == 0
    assert row[1] == pytest.approx(0.0)


@pytest.mark.unit
def test_dates_with_trades_returns_only_dates_with_closed_trades(tmp_db_path):
    """An open trade (exit_time NULL) must not count — recompute would
    pick up an in-progress day and write zeros over a future correct
    summary."""
    with _conn(tmp_db_path) as conn:
        _seed_trade(conn, ticker="SPY", exit_date="2026-04-30",
                    gross=10.0, pnl=10.0)
        conn.execute(
            """INSERT INTO trades (
                ticker, exchange, currency, side,
                entry_time, entry_price, quantity,
                hold_type, phase, strategy_id
            ) VALUES ('QQQ', 'US', 'USD', 'BUY',
                      '2026-05-06T15:00:00', 200.0, 5,
                      'swing', 3, 'overnight_drift')""",
        )
        conn.commit()

        # Use a wide enough days_back to reach back from "now" to test data.
        # Test fixtures use 2026-04-30 / 2026-05-06; days_back=10000 covers
        # any wall-clock date.
        dates = _dates_with_trades(conn, days_back=10000)
    assert "2026-04-30" in dates
    assert "2026-05-06" not in dates

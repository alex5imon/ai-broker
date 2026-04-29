"""Tests for trading_bot.self_improve.postmortem."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from trading_bot.self_improve.postmortem import (
    StrategyStats,
    compute_window_stats,
    summarize_all,
)


def _insert_trade(
    conn,
    *,
    ticker: str = "SPY",
    strategy_id: str = "mean_reversion",
    entry_time: datetime,
    exit_time: datetime | None,
    exit_reason: str | None,
    net_pnl: float | None,
    quantity: int = 10,
    entry_price: float = 100.0,
    exit_price: float | None = 101.0,
) -> None:
    conn.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side, entry_time, entry_price,
            quantity, exit_time, exit_price, exit_reason,
            gross_pnl, net_pnl, hold_type, phase, strategy_id
        )
        VALUES (?, 'NASDAQ', 'USD', 'long', ?, ?, ?, ?, ?, ?, ?, ?, 'intraday', 1, ?)
        """,
        (
            ticker,
            entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            entry_price,
            quantity,
            exit_time.strftime("%Y-%m-%d %H:%M:%S") if exit_time else None,
            exit_price,
            exit_reason,
            net_pnl,
            net_pnl,
            strategy_id,
        ),
    )
    conn.commit()


@pytest.mark.unit
def test_empty_window_returns_zero_stats(tmp_db):
    stats = compute_window_stats(tmp_db, "mean_reversion", window_days=20)
    assert stats.n_trades == 0
    assert stats.win_rate == 0.0
    assert stats.profit_factor is None
    assert stats.total_pnl_usd == 0.0


@pytest.mark.unit
def test_open_trades_excluded(tmp_db):
    now = datetime.now(timezone.utc)
    _insert_trade(
        tmp_db,
        entry_time=now - timedelta(hours=2),
        exit_time=None,
        exit_reason=None,
        net_pnl=None,
    )
    stats = compute_window_stats(tmp_db, "mean_reversion", window_days=20)
    assert stats.n_trades == 0


@pytest.mark.unit
def test_trades_outside_window_excluded(tmp_db):
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    _insert_trade(
        tmp_db,
        entry_time=old,
        exit_time=old + timedelta(minutes=30),
        exit_reason="take_profit",
        net_pnl=10.0,
    )
    stats = compute_window_stats(tmp_db, "mean_reversion", window_days=20)
    assert stats.n_trades == 0


@pytest.mark.unit
def test_stats_aggregate_correctly(tmp_db):
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=5)
    # 2 wins (+12, +8), 2 losses (-10, -4)
    for i, (pnl, reason, hold_min) in enumerate([
        (12.0, "trailing_stop", 60),
        (8.0, "take_profit", 30),
        (-10.0, "stop_loss", 15),
        (-4.0, "wind_down", 90),
    ]):
        entry = base + timedelta(hours=i)
        _insert_trade(
            tmp_db,
            entry_time=entry,
            exit_time=entry + timedelta(minutes=hold_min),
            exit_reason=reason,
            net_pnl=pnl,
        )

    stats = compute_window_stats(tmp_db, "mean_reversion", window_days=20)
    assert stats.n_trades == 4
    assert stats.win_rate == 0.5
    assert stats.total_pnl_usd == pytest.approx(6.0)
    assert stats.avg_win_usd == pytest.approx(10.0)
    assert stats.avg_loss_usd == pytest.approx(-7.0)
    # PF = wins / |losses| = 20 / 14
    assert stats.profit_factor == pytest.approx(20 / 14)
    assert stats.exit_share("stop_loss") == 0.25
    assert stats.exit_share("take_profit") == 0.25
    assert stats.exit_share("nonexistent") == 0.0
    assert stats.avg_hold_minutes == pytest.approx((60 + 30 + 15 + 90) / 4)


@pytest.mark.unit
def test_profit_factor_when_only_wins(tmp_db):
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=2)
    _insert_trade(
        tmp_db,
        entry_time=base,
        exit_time=base + timedelta(minutes=30),
        exit_reason="take_profit",
        net_pnl=5.0,
    )
    stats = compute_window_stats(tmp_db, "mean_reversion", window_days=20)
    assert stats.profit_factor == float("inf")


@pytest.mark.unit
def test_summarize_all_partitions_by_strategy(tmp_db):
    now = datetime.now(timezone.utc)
    base = now - timedelta(days=1)
    _insert_trade(
        tmp_db,
        entry_time=base,
        exit_time=base + timedelta(minutes=10),
        exit_reason="take_profit",
        net_pnl=5.0,
        strategy_id="mean_reversion",
    )
    _insert_trade(
        tmp_db,
        entry_time=base,
        exit_time=base + timedelta(hours=18),
        exit_reason="stop_loss",
        net_pnl=-3.0,
        strategy_id="overnight_drift",
    )
    out = summarize_all(
        tmp_db, ["mean_reversion", "overnight_drift", "trend_following"], 20,
    )
    assert out["mean_reversion"].n_trades == 1
    assert out["overnight_drift"].n_trades == 1
    assert out["trend_following"].n_trades == 0


@pytest.mark.unit
def test_invalid_window_raises(tmp_db):
    with pytest.raises(ValueError):
        compute_window_stats(tmp_db, "mean_reversion", window_days=0)


@pytest.mark.unit
def test_stats_dataclass_is_immutable():
    s = StrategyStats(
        strategy_id="x",
        window_days=10,
        n_trades=0,
        win_rate=0.0,
        profit_factor=None,
        total_pnl_usd=0.0,
        avg_win_usd=0.0,
        avg_loss_usd=0.0,
        avg_hold_minutes=0.0,
    )
    with pytest.raises(Exception):
        s.n_trades = 5  # type: ignore[misc]

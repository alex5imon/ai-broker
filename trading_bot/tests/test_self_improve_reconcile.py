"""Tests for trading_bot.self_improve.reconcile.

Focus is the classification table — every label in PositionClass and TradeClass
should have a deterministic test that pins its meaning. Alpaca state is built
by hand as plain dataclasses so no network is touched.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


from trading_bot.self_improve.reconcile import (
    AlpacaOrderRec,
    AlpacaPosition,
    AlpacaState,
    PositionClass,
    TradeClass,
    build_report,
    classify_position,
    classify_trade,
    load_db_positions,
    load_db_trades,
    render_markdown,
    _position_lookup,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ENABLED_MAP: dict[str, bool] = {
    "mean_reversion": True,
    "overnight_drift": True,
    "breakout": False,
    "trend_following": False,
    "sentiment_combo": False,
}

ENTRY_TIME = datetime(2026, 4, 28, 10, 30, 0, tzinfo=timezone.utc)


def _make_state(
    *,
    positions: list[AlpacaPosition] | None = None,
    orders: list[AlpacaOrderRec] | None = None,
) -> AlpacaState:
    by_sym: dict[str, AlpacaPosition] = {p.symbol: p for p in positions or []}
    by_id: dict[str, AlpacaOrderRec] = {o.order_id: o for o in orders or []}
    fills: dict[str, list[AlpacaOrderRec]] = {}
    for o in orders or []:
        if o.filled_at is not None and o.filled_qty > 0:
            fills.setdefault(o.symbol, []).append(o)
    return AlpacaState(
        account_id="PA-TEST",
        is_paper=True,
        fetched_at=datetime.now(tz=timezone.utc),
        positions_by_symbol=by_sym,
        orders_by_id=by_id,
        fills_by_symbol={k: tuple(v) for k, v in fills.items()},
    )


def _db_position(
    *,
    pos_id: int = 1,
    ticker: str = "SPY",
    qty: int = 10,
    status: str = "POSITION_OPEN",
    strategy_id: str | None = "mean_reversion",
    entry_time: datetime = ENTRY_TIME,
    alpaca_order_id: str | None = None,
    alpaca_stop_order_id: str | None = None,
    alpaca_target_order_id: str | None = None,
    alpaca_trail_order_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": pos_id,
        "ticker": ticker,
        "exchange": "ARCA",
        "currency": "USD",
        "quantity": qty,
        "entry_price": 100.0,
        "entry_time": entry_time.isoformat(),
        "status": status,
        "strategy_id": strategy_id,
        "alpaca_order_id": alpaca_order_id,
        "alpaca_stop_order_id": alpaca_stop_order_id,
        "alpaca_target_order_id": alpaca_target_order_id,
        "alpaca_trail_order_id": alpaca_trail_order_id,
        "hold_type": "intraday",
        "phase": 1,
    }


def _buy_fill(
    *,
    order_id: str = "buy-1",
    symbol: str = "SPY",
    qty: float = 10.0,
    at: datetime = ENTRY_TIME,
) -> AlpacaOrderRec:
    return AlpacaOrderRec(
        order_id=order_id,
        symbol=symbol,
        side="buy",
        status="filled",
        qty=qty,
        filled_qty=qty,
        filled_avg_price=100.0,
        filled_at=at,
        submitted_at=at - timedelta(seconds=2),
    )


def _sell_fill(
    *,
    order_id: str = "sell-1",
    symbol: str = "SPY",
    qty: float = 10.0,
    at: datetime | None = None,
) -> AlpacaOrderRec:
    return AlpacaOrderRec(
        order_id=order_id,
        symbol=symbol,
        side="sell",
        status="filled",
        qty=qty,
        filled_qty=qty,
        filled_avg_price=101.5,
        filled_at=at if at is not None else ENTRY_TIME + timedelta(hours=2),
        submitted_at=ENTRY_TIME + timedelta(hours=2),
    )


# ---------------------------------------------------------------------------
# Open-side classification
# ---------------------------------------------------------------------------


def test_actual_open_when_db_and_alpaca_agree() -> None:
    pos = _db_position()
    state = _make_state(
        positions=[AlpacaPosition("SPY", qty=10.0, avg_entry_price=100.0, market_value=1000.0)],
    )
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.ACTUAL_OPEN


def test_mismatch_qty_when_alpaca_holds_different_quantity() -> None:
    pos = _db_position(qty=10)
    state = _make_state(
        positions=[AlpacaPosition("SPY", qty=7.0, avg_entry_price=100.0, market_value=700.0)],
    )
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.MISMATCH_QTY
    assert "diff=" in out.evidence


def test_orphan_disabled_when_strategy_is_disabled_in_config() -> None:
    pos = _db_position(strategy_id="breakout")
    state = _make_state(
        positions=[AlpacaPosition("SPY", qty=10.0, avg_entry_price=100.0, market_value=1000.0)],
    )
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.ORPHAN_DISABLED


def test_orphan_unknown_when_strategy_is_null_or_unknown() -> None:
    state = _make_state(
        positions=[AlpacaPosition("SPY", qty=10.0, avg_entry_price=100.0, market_value=1000.0)],
    )
    null_pos = _db_position(strategy_id=None)
    unknown_pos = _db_position(strategy_id="unknown")
    assert (
        classify_position(null_pos, state, ENABLED_MAP).classification
        is PositionClass.ORPHAN_UNKNOWN
    )
    assert (
        classify_position(unknown_pos, state, ENABLED_MAP).classification
        is PositionClass.ORPHAN_UNKNOWN
    )


def test_orphan_not_held_when_db_open_but_alpaca_zero() -> None:
    pos = _db_position()
    state = _make_state(positions=[])
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.ORPHAN_NOT_HELD


# ---------------------------------------------------------------------------
# Closed-side classification
# ---------------------------------------------------------------------------


def test_actual_fill_when_both_entry_and_exit_fills_exist() -> None:
    pos = _db_position(status="CLOSED")
    state = _make_state(orders=[_buy_fill(), _sell_fill()])
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.ACTUAL_FILL


def test_actual_fill_uses_explicit_alpaca_order_id_when_present() -> None:
    pos = _db_position(status="CLOSED", alpaca_order_id="explicit-buy")
    state = _make_state(
        orders=[
            _buy_fill(order_id="explicit-buy"),
            _sell_fill(),
        ]
    )
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.ACTUAL_FILL
    assert "explicit-buy" in out.evidence


def test_phantom_close_when_no_buy_fill_exists() -> None:
    pos = _db_position(status="CLOSED")
    state = _make_state(orders=[])
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.PHANTOM_CLOSE


def test_closed_no_exit_when_buy_filled_but_no_sell() -> None:
    pos = _db_position(status="CLOSED")
    state = _make_state(orders=[_buy_fill()])  # no sell
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.CLOSED_NO_EXIT


def test_closed_with_unparseable_entry_time_classes_as_phantom() -> None:
    pos = _db_position(status="CLOSED")
    pos["entry_time"] = "not-a-real-date"
    state = _make_state(orders=[])
    out = classify_position(pos, state, ENABLED_MAP)
    assert out.classification is PositionClass.PHANTOM_CLOSE


# ---------------------------------------------------------------------------
# Trades classification
# ---------------------------------------------------------------------------


def _trade_row(
    *,
    trade_id: int = 1,
    ticker: str = "SPY",
    strategy_id: str | None = None,
    exit_time: str | None = None,
    entry_time: datetime = ENTRY_TIME,
) -> dict[str, Any]:
    return {
        "id": trade_id,
        "ticker": ticker,
        "exchange": "ARCA",
        "currency": "USD",
        "side": "long",
        "entry_time": entry_time.isoformat(),
        "entry_price": 100.0,
        "quantity": 10,
        "exit_time": exit_time,
        "exit_price": 101.5 if exit_time else None,
        "exit_reason": "stop_loss" if exit_time else None,
        "net_pnl": 15.0 if exit_time else None,
        "hold_type": "intraday",
        "phase": 1,
        "strategy_id": strategy_id,
    }


def test_entry_only_phantom_when_strategy_and_exit_both_null() -> None:
    trade = _trade_row()
    paired_pos = _db_position(status="CLOSED", strategy_id="mean_reversion")
    lookup = _position_lookup([paired_pos])
    out = classify_trade(trade, lookup)
    assert out.classification is TradeClass.ENTRY_ONLY_PHANTOM
    # Confirms which paired-position attribution would have fixed it.
    assert "mean_reversion" in out.evidence


def test_missing_strategy_when_exit_present_but_strategy_null() -> None:
    trade = _trade_row(exit_time="2026-04-28T13:00:00+00:00", strategy_id=None)
    out = classify_trade(trade, {})
    assert out.classification is TradeClass.MISSING_STRATEGY


def test_missing_exit_when_strategy_present_but_exit_null() -> None:
    trade = _trade_row(strategy_id="mean_reversion", exit_time=None)
    out = classify_trade(trade, {})
    assert out.classification is TradeClass.MISSING_EXIT


def test_complete_when_both_present() -> None:
    trade = _trade_row(
        strategy_id="mean_reversion",
        exit_time="2026-04-28T13:00:00+00:00",
    )
    out = classify_trade(trade, {})
    assert out.classification is TradeClass.COMPLETE


# ---------------------------------------------------------------------------
# Report assembly + rendering
# ---------------------------------------------------------------------------


def _seed_realistic_db(conn: sqlite3.Connection) -> None:
    """Insert a small realistic mix mirroring the bot-db artifact."""
    # 1 actual open
    conn.execute(
        "INSERT INTO positions (ticker, exchange, currency, quantity, "
        "entry_price, entry_time, status, hold_type, phase, strategy_id, "
        "alpaca_order_id) VALUES "
        "('SPY','ARCA','USD',10,100.0,?, 'POSITION_OPEN','intraday',1,"
        "'mean_reversion','buy-spy')",
        (ENTRY_TIME.isoformat(),),
    )
    # 1 orphan disabled (breakout)
    conn.execute(
        "INSERT INTO positions (ticker, exchange, currency, quantity, "
        "entry_price, entry_time, status, hold_type, phase, strategy_id) "
        "VALUES ('XLF','ARCA','USD',5,40.0,?,'STOP_ACTIVE',"
        "'intraday',1,'breakout')",
        (ENTRY_TIME.isoformat(),),
    )
    # 1 orphan unknown
    conn.execute(
        "INSERT INTO positions (ticker, exchange, currency, quantity, "
        "entry_price, entry_time, status, hold_type, phase, strategy_id) "
        "VALUES ('QQQ','NASDAQ','USD',2,400.0,?,'POSITION_OPEN',"
        "'intraday',1,'unknown')",
        (ENTRY_TIME.isoformat(),),
    )
    # 1 phantom close (CLOSED but no buy fill)
    conn.execute(
        "INSERT INTO positions (ticker, exchange, currency, quantity, "
        "entry_price, entry_time, status, hold_type, phase, strategy_id) "
        "VALUES ('IWM','ARCA','USD',8,200.0,?,'CLOSED','intraday',1,"
        "'mean_reversion')",
        (ENTRY_TIME.isoformat(),),
    )
    # Trades: 1 entry-only phantom + 1 complete
    conn.execute(
        "INSERT INTO trades (ticker, exchange, currency, side, entry_time, "
        "entry_price, quantity, hold_type, phase) VALUES "
        "('SPY','ARCA','USD','long',?,100.0,10,'intraday',1)",
        (ENTRY_TIME.isoformat(),),
    )
    conn.execute(
        "INSERT INTO trades (ticker, exchange, currency, side, entry_time, "
        "entry_price, quantity, exit_time, exit_price, exit_reason, "
        "net_pnl, hold_type, phase, strategy_id) VALUES "
        "('IWM','ARCA','USD','long',?,200.0,8,?,201.0,'stop_loss',8.0,"
        "'intraday',1,'mean_reversion')",
        (ENTRY_TIME.isoformat(), (ENTRY_TIME + timedelta(hours=1)).isoformat()),
    )
    conn.commit()


def test_build_report_groups_findings_and_renders_clean_markdown(tmp_db) -> None:
    _seed_realistic_db(tmp_db)
    state = _make_state(
        positions=[AlpacaPosition("SPY", qty=10.0, avg_entry_price=100.0, market_value=1000.0)],
        orders=[],  # nothing for IWM => phantom close
    )
    db_positions = load_db_positions(tmp_db)
    db_trades = load_db_trades(tmp_db)
    report = build_report(
        db_path=":memory:",
        state=state,
        db_positions=db_positions,
        db_trades=db_trades,
        strategy_enabled=ENABLED_MAP,
        since=ENTRY_TIME - timedelta(days=30),
        until=ENTRY_TIME + timedelta(days=1),
    )
    classes = {pf.classification for pf in report.position_findings}
    assert PositionClass.ACTUAL_OPEN in classes
    assert PositionClass.ORPHAN_DISABLED in classes
    assert PositionClass.ORPHAN_UNKNOWN in classes
    assert PositionClass.PHANTOM_CLOSE in classes

    md = render_markdown(report)
    assert "# Reconciliation report" in md
    assert "ACTUAL_OPEN" in md
    assert "ORPHAN_DISABLED" in md
    assert "PHANTOM_CLOSE" in md
    assert "Bug-hypothesis confirmation" in md
    # Account + paper banner.
    assert "PA-TEST" in md
    assert "paper" in md


def test_build_report_with_no_db_rows_produces_zero_count_table(tmp_db) -> None:
    state = _make_state()
    report = build_report(
        db_path=":memory:",
        state=state,
        db_positions=[],
        db_trades=[],
        strategy_enabled=ENABLED_MAP,
        since=ENTRY_TIME - timedelta(days=30),
        until=ENTRY_TIME + timedelta(days=1),
    )
    md = render_markdown(report)
    # Every classification row should be present with 0.
    for cls in PositionClass:
        assert f"| {cls.value} | 0 |" in md


# ---------------------------------------------------------------------------
# Strategy-enabled-map loader
# ---------------------------------------------------------------------------


def test_load_strategy_enabled_map_reads_real_config() -> None:
    from trading_bot.self_improve.reconcile import load_strategy_enabled_map

    enabled: dict[str, bool] = load_strategy_enabled_map("config.yaml")
    # Mean reversion must be on; breakout/trend_following are off after the
    # 2026-04-28 regime-aware rebalance (PR #12).
    assert enabled.get("mean_reversion") is True
    assert enabled.get("overnight_drift") is True
    assert enabled.get("breakout") is False
    assert enabled.get("trend_following") is False

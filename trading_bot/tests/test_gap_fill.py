"""Unit tests for GapFillStrategy (sleeve #9, ai-broker#48)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.constants import HoldType
from trading_bot.strategy.strategies.gap_fill import GapFillStrategy

ET = ZoneInfo("US/Eastern")
TODAY = date(2026, 3, 16)        # Monday
PRIOR_DAY = date(2026, 3, 13)    # Friday
PRIOR_CLOSE = 100.0
CASH = 100_000.0


def _strat(**overrides: Any) -> GapFillStrategy:
    cfg: dict[str, Any] = {
        "max_positions": 3,
        "entry_time_et": "09:35",
        "time_stop_et": "14:00",
        "min_gap_pct": 0.005,
        "gap_atr_multiplier": 0.5,
        "overnight_atr_period": 14,
        "stop_atr_multiplier": 1.5,
        "stop_pct_floor": 0.008,
        "risk_per_trade_pct": 0.005,
        "max_position_pct": 0.33,
        "fractional_shares": True,
    }
    cfg.update(overrides)
    return GapFillStrategy(cfg)


def _daily(prior_close: float = PRIOR_CLOSE, atr_pct: float = 0.005, n: int = 20) -> pd.DataFrame:
    """Daily bars ending the Friday before TODAY, with a controllable ATR%.

    Constant close → true range per bar ≈ 2*h, so ATR ≈ 2*h and
    overnight_atr_pct ≈ atr_pct when h = atr_pct*prior_close/2.
    """
    end = PRIOR_DAY
    dates: list[date] = []
    d = end
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(d)
        d -= timedelta(days=1)
    dates = sorted(dates)
    ts = [datetime(x.year, x.month, x.day, 16, 0, tzinfo=ET) for x in dates]
    h = atr_pct * prior_close / 2.0
    data = {
        "open": [prior_close] * n,
        "high": [prior_close + h] * n,
        "low": [prior_close - h] * n,
        "close": [prior_close] * n,
        "volume": [1_000_000] * n,
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(ts, tz=ET))


def _today_5min(
    *, gap_pct: float, n_bars: int = 2, day: date = TODAY,
    first_bar: time = time(9, 30),
) -> pd.DataFrame:
    """Today's 5-min bars. The first (09:30) bar's OPEN encodes the gap; the
    entry fires on the 09:35 bar (default n_bars=2 → last bar labelled 09:35).
    All opens/closes are the session open for deterministic sizing."""
    session_open = PRIOR_CLOSE * (1.0 + gap_pct)
    start = datetime(day.year, day.month, day.day, first_bar.hour, first_bar.minute, tzinfo=ET)
    ts = [start + timedelta(minutes=5 * i) for i in range(n_bars)]
    opens = [session_open] * n_bars
    closes = [session_open] * n_bars
    data = {
        "open": opens,
        "high": [o + 0.05 for o in opens],
        "low": [o - 0.05 for o in opens],
        "close": closes,
        "volume": [100_000] * n_bars,
    }
    return pd.DataFrame(data, index=pd.DatetimeIndex(ts, tz=ET))


def _entry(strat: GapFillStrategy, df5: pd.DataFrame, dfd: pd.DataFrame):
    return strat.evaluate_entry(
        ticker="SPY", exchange="NYSE", df_5min=df5, df_daily=dfd,
        current_price=float(df5["close"].iloc[-1]), available_cash=CASH,
    )


# ---------------------------------------------------------------------------
# Threshold gating
# ---------------------------------------------------------------------------

def test_no_entry_below_gap_threshold() -> None:
    # gap 0.4%, ATR 0.5% → threshold max(0.5%, 0.25%) = 0.5% → 0.4% rejected.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.004), _daily(atr_pct=0.005))
    assert d is None


def test_no_entry_below_adaptive_threshold() -> None:
    # gap 0.6%, ATR 1.5% → threshold max(0.5%, 0.75%) = 0.75% → 0.6% rejected.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.006), _daily(atr_pct=0.015))
    assert d is None


def test_short_entry_on_gap_up() -> None:
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.01), _daily(atr_pct=0.005))
    assert d is not None
    assert d.direction == "short"
    assert d.stop_price > d.entry_price          # short stop sits above entry
    assert d.target_price == pytest.approx(PRIOR_CLOSE, abs=1e-4)
    assert d.hold_type == HoldType.INTRADAY


def test_long_entry_on_gap_down() -> None:
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=-0.01), _daily(atr_pct=0.005))
    assert d is not None
    assert d.direction == "long"
    assert d.stop_price < d.entry_price          # long stop sits below entry


def test_target_at_prior_close() -> None:
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=-0.01), _daily(prior_close=100.0, atr_pct=0.005))
    assert d is not None
    assert d.target_price == pytest.approx(100.0, abs=1e-4)


# ---------------------------------------------------------------------------
# Stop sizing
# ---------------------------------------------------------------------------

def test_stop_at_atr_distance() -> None:
    # Large ATR (2%) → stop distance = max(1.5*2%, 0.8%) = 3%.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=-0.04), _daily(atr_pct=0.02))
    assert d is not None
    stop_dist = (d.entry_price - d.stop_price) / d.entry_price
    assert stop_dist == pytest.approx(0.03, abs=2e-3)


def test_stop_floor_enforced() -> None:
    # Pathologically tiny ATR (0.1%) → 1.5*0.1% = 0.15% < 0.8% floor → 0.8%.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=-0.01), _daily(atr_pct=0.001))
    assert d is not None
    stop_dist = (d.entry_price - d.stop_price) / d.entry_price
    assert stop_dist == pytest.approx(0.008, abs=2e-3)


# ---------------------------------------------------------------------------
# Entry window / one-per-day / convention
# ---------------------------------------------------------------------------

def test_one_entry_per_ticker_per_day() -> None:
    # Past the entry bar (last bar 09:40) → no entry. The entry fires only on
    # the single 09:35 bar, so the sleeve enters at most once per session.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.01, n_bars=3), _daily(atr_pct=0.005))
    assert d is None


def test_no_entry_outside_window() -> None:
    # Late/halted open: first bar labelled 09:45 → no 09:35 bar → skip.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.01, first_bar=time(9, 45)), _daily(atr_pct=0.005))
    assert d is None


def test_5min_bar_timestamp_convention_correct() -> None:
    """Off-by-one guard for the LEFT-labelled 5-min grid.

    We act on the bar labelled 09:35 (the first inside the execution window)
    and measure the gap from the session OPEN — the 09:30 bar's open. We must
    NOT fire on the 09:30 bar (before the window) nor on a later bar (09:40).
    """
    s = _strat()

    # Only the 09:30 open bar → before the entry bar → no fire.
    df_open_only = _today_5min(gap_pct=0.01, n_bars=1)
    assert df_open_only.index[-1].time() == time(9, 30)
    assert _entry(s, df_open_only, _daily(atr_pct=0.005)) is None

    # 09:35 is the last bar → fire; gap measured from the 09:30 open.
    df_two = _today_5min(gap_pct=0.01, n_bars=2)
    assert df_two.index[-1].time() == time(9, 35)
    d = _entry(s, df_two, _daily(atr_pct=0.005))
    assert d is not None
    assert d.signals["session_open"] == pytest.approx(PRIOR_CLOSE * 1.01, abs=1e-4)

    # 09:40 is the last bar → past the entry bar → no fire.
    df_three = _today_5min(gap_pct=0.01, n_bars=3)
    assert df_three.index[-1].time() == time(9, 40)
    assert _entry(s, df_three, _daily(atr_pct=0.005)) is None


def test_min_warmup_bars_relaxed() -> None:
    # gap_fill must relax the backtester's 10-bar intraday warm-up so its
    # bar-index-1 (09:35) entry isn't suppressed.
    assert _strat().min_warmup_bars() == 1


def test_uses_adjusted_close_for_prior_close() -> None:
    # The gap is measured against the daily (adjusted) close, not anything in
    # the 5-min frame. Prior close 100, open 101 → +1% gap recorded.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.01), _daily(prior_close=100.0, atr_pct=0.005))
    assert d is not None
    assert d.signals["prior_close"] == pytest.approx(100.0, abs=1e-4)
    assert d.signals["gap_pct"] == pytest.approx(0.01, abs=1e-4)


def test_short_history_skips_ticker() -> None:
    # Fewer than overnight_atr_period+1 daily bars → skip, no crash.
    s = _strat()
    d = _entry(s, _today_5min(gap_pct=0.01), _daily(atr_pct=0.005, n=10))
    assert d is None


def test_state_resets_at_session_start() -> None:
    # A fresh first bar on a different session fires again (no stale latch).
    s = _strat()
    next_day = date(2026, 3, 17)
    df = _today_5min(gap_pct=0.01, day=next_day)
    # Daily history still ends before TODAY, which is before next_day — valid.
    d = _entry(s, df, _daily(atr_pct=0.005))
    assert d is not None
    assert d.direction == "short"


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------

def _exit_bars(bar_time: time) -> pd.DataFrame:
    ts = [datetime(TODAY.year, TODAY.month, TODAY.day, bar_time.hour, bar_time.minute, tzinfo=ET)]
    return pd.DataFrame(
        {"open": [100.0], "high": [100.0], "low": [100.0], "close": [100.0],
         "volume": [1000]},
        index=pd.DatetimeIndex(ts, tz=ET),
    )


def test_time_stop_at_14_00() -> None:
    s = _strat()
    pos = {"direction": "short", "entry_price": 101.0, "stop_price": 103.0,
           "target_price": 100.0}
    sig = s.evaluate_exit(pos, current_price=101.0, df_5min=_exit_bars(time(14, 0)))
    assert sig.should_exit
    assert sig.reason == "time_stop"


def test_no_time_stop_before_14_00() -> None:
    s = _strat()
    pos = {"direction": "short", "entry_price": 101.0, "stop_price": 103.0,
           "target_price": 100.0}
    sig = s.evaluate_exit(pos, current_price=101.0, df_5min=_exit_bars(time(13, 55)))
    assert not sig.should_exit


def test_short_target_exit_at_prior_close() -> None:
    s = _strat()
    pos = {"side": "SELL", "entry_price": 101.0, "stop_price": 103.0,
           "target_price": 100.0}
    sig = s.evaluate_exit(pos, current_price=99.9, df_5min=_exit_bars(time(11, 0)))
    assert sig.should_exit
    assert sig.reason == "take_profit"


def test_short_stop_exit_above_entry() -> None:
    s = _strat()
    pos = {"side": "SELL", "entry_price": 101.0, "stop_price": 103.0,
           "target_price": 100.0}
    sig = s.evaluate_exit(pos, current_price=103.5, df_5min=_exit_bars(time(11, 0)))
    assert sig.should_exit
    assert sig.reason == "stop_loss"


def test_long_target_exit_at_prior_close() -> None:
    s = _strat()
    pos = {"direction": "long", "entry_price": 99.0, "stop_price": 98.0,
           "target_price": 100.0}
    sig = s.evaluate_exit(pos, current_price=100.1, df_5min=_exit_bars(time(11, 0)))
    assert sig.should_exit
    assert sig.reason == "take_profit"


# ---------------------------------------------------------------------------
# End-to-end through the real backtester (wiring: warm-up hook + exec window
# + short entry → backtester short path → target cover)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gap_fill_short_fills_target_through_backtester() -> None:
    from unittest.mock import patch

    from trading_bot.config import Config
    from trading_bot.multi_strategy_backtest import MultiStrategyBacktester

    config = Config.load("config.yaml")
    engine = MultiStrategyBacktester(config)
    # Run only gap_fill regardless of which sleeves config enables.
    engine._strategies = [_strat()]

    # A full gap-up-then-revert session: 09:30 opens +1.5%, drifts back
    # through the prior close (100) so the short covers at target.
    start = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 30, tzinfo=ET)
    n = 78  # 09:30 → ~15:55
    ts = [start + timedelta(minutes=5 * i) for i in range(n)]
    # Price path: 101.5 → 99.0 over the first 20 bars, then flat at 99.
    prices = [max(99.0, 101.5 - 0.125 * i) for i in range(n)]
    df5 = pd.DataFrame(
        {
            "open": prices,
            "high": [p + 0.05 for p in prices],
            "low": [p - 0.05 for p in prices],
            "close": prices,
            "volume": [100_000] * n,
        },
        index=pd.DatetimeIndex(ts, tz=ET),
    )
    mock_data = {"SPY": {"intraday": df5, "daily": _daily(atr_pct=0.005)}}

    with patch.object(engine, "_load_day_data", return_value=mock_data):
        result = await engine.run(TODAY, TODAY, cash_per_strategy_usd=100_000.0)

    sr = result.strategies[0]
    assert sr.total_trades == 1, "gap_fill should open exactly one short"
    trade = sr.trades[0]
    assert trade.direction == "short"
    assert trade.exit_reason == "take_profit"
    assert trade.net_pnl_usd > 0, "a short that covers below entry must profit"

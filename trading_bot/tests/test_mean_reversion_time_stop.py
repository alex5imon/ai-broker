"""Tests for MeanReversionStrategy.evaluate_exit time_stop logic.

Bug: mean_reversion positions could hold indefinitely at breakeven because
evaluate_exit had no time_stop.  ExitManager.check_time_stop (5 trading-day
default) was only wired for legacy/untagged positions, never for
strategy-tagged positions routed through strategy_manager.check_exits.

These tests pin:
A. A position held for exactly max_hold_days triggers time_stop.
B. A position held for fewer than max_hold_days does NOT trigger time_stop.
C. stop_loss still fires before the time_stop when price is below stop.
D. take_profit still fires before the time_stop when price is above target.
E. max_hold_days is configurable from the strategy config dict.
F. A missing / malformed entry_time does NOT raise — time_stop is skipped.
G. Weekends are not counted as trading days (calendar-aware).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy

ET = ZoneInfo("US/Eastern")


def _strategy(config: dict[str, Any] | None = None) -> MeanReversionStrategy:
    return MeanReversionStrategy(config=config or {})


def _position(
    entry_time: datetime,
    entry_price: float = 100.0,
    stop_price: float = 97.0,   # 3% below — safely below current price in most tests
    target_price: float = 110.0,  # 10% above — safely above current price in most tests
) -> dict[str, Any]:
    return {
        "id": 1,
        "ticker": "XLF",
        "entry_time": entry_time.isoformat(),
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
    }


def _now_et(**delta_kwargs: Any) -> datetime:
    """Return a timezone-aware ET datetime offset from a fixed anchor."""
    # Anchor: a known Thursday trading day (not a holiday).
    anchor = datetime(2026, 5, 22, 10, 0, 0, tzinfo=ET)
    return anchor + timedelta(**delta_kwargs)


# ---------------------------------------------------------------------------
# A. time_stop fires at exactly max_hold_days
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_time_stop_fires_at_max_hold_days() -> None:
    """A position open for exactly max_hold_days trading days triggers time_stop."""
    strategy = _strategy({"max_hold_days": 3})
    # Entry 3 trading days before "now".  Mon 2026-05-18 → now Thu 2026-05-21
    # (Mon=1, Tue=2, Wed=3 trading days elapsed).
    entry = datetime(2026, 5, 18, 15, 30, 0, tzinfo=ET)
    now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=ET)  # 3 trading days later

    pos = _position(entry_time=entry, stop_price=90.0, target_price=120.0)

    with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.now.return_value = now

        signal = strategy.evaluate_exit(
            position=pos,
            current_price=100.5,  # between stop and target
        )

    assert signal.should_exit is True
    assert signal.reason == "time_stop"


# ---------------------------------------------------------------------------
# B. No time_stop before max_hold_days
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_time_stop_does_not_fire_before_max_hold_days() -> None:
    """A position open for fewer than max_hold_days does NOT trigger time_stop."""
    strategy = _strategy({"max_hold_days": 5})
    # Entry 2 trading days before now
    entry = datetime(2026, 5, 20, 9, 30, 0, tzinfo=ET)
    now = datetime(2026, 5, 22, 10, 0, 0, tzinfo=ET)  # 2 trading days later

    pos = _position(entry_time=entry, stop_price=90.0, target_price=120.0)

    with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.now.return_value = now

        signal = strategy.evaluate_exit(
            position=pos,
            current_price=100.5,
        )

    assert signal.should_exit is False


# ---------------------------------------------------------------------------
# C. stop_loss fires before time_stop
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_stop_loss_fires_before_time_stop() -> None:
    """stop_loss is checked first and fires even when max_hold_days is exceeded."""
    strategy = _strategy({"max_hold_days": 1})
    entry = datetime(2026, 5, 20, 9, 30, 0, tzinfo=ET)
    now = datetime(2026, 5, 22, 10, 0, 0, tzinfo=ET)  # 2 trading days → past max

    stop = 98.0
    pos = _position(entry_time=entry, stop_price=stop, target_price=120.0)

    with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.now.return_value = now

        signal = strategy.evaluate_exit(
            position=pos,
            current_price=97.5,  # below stop
        )

    assert signal.should_exit is True
    assert signal.reason == "stop_loss"


# ---------------------------------------------------------------------------
# D. take_profit fires before time_stop
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_take_profit_fires_before_time_stop() -> None:
    """take_profit is checked before time_stop and fires when price exceeds target."""
    strategy = _strategy({"max_hold_days": 1})
    entry = datetime(2026, 5, 20, 9, 30, 0, tzinfo=ET)
    now = datetime(2026, 5, 22, 10, 0, 0, tzinfo=ET)  # 2 days → past max

    target = 110.0
    pos = _position(entry_time=entry, stop_price=90.0, target_price=target)

    with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.now.return_value = now

        signal = strategy.evaluate_exit(
            position=pos,
            current_price=115.0,  # above target
        )

    assert signal.should_exit is True
    assert signal.reason == "take_profit"


# ---------------------------------------------------------------------------
# E. max_hold_days is configurable
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_max_hold_days_configurable() -> None:
    """max_hold_days read from config overrides the 5-day default."""
    strategy_short = _strategy({"max_hold_days": 2})
    strategy_long = _strategy({"max_hold_days": 10})

    assert strategy_short._max_hold_days == 2
    assert strategy_long._max_hold_days == 10

    # 3 trading days elapsed — fires for max=2, not for max=10
    entry = datetime(2026, 5, 19, 9, 30, 0, tzinfo=ET)   # Mon
    now = datetime(2026, 5, 22, 10, 0, 0, tzinfo=ET)     # Thu = 3 trading days

    pos = _position(entry_time=entry, stop_price=90.0, target_price=120.0)

    for strategy, expect_exit in [(strategy_short, True), (strategy_long, False)]:
        with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            mock_dt.now.return_value = now
            sig = strategy.evaluate_exit(position=pos, current_price=100.5)
        assert sig.should_exit is expect_exit, (
            f"max_hold_days={strategy._max_hold_days}: expected should_exit={expect_exit}"
        )


# ---------------------------------------------------------------------------
# F. Missing / malformed entry_time does not raise
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_time_stop_skipped_when_entry_time_missing() -> None:
    """Absent entry_time silently skips the time_stop; no exception raised."""
    strategy = _strategy({"max_hold_days": 1})
    pos: dict[str, Any] = {
        "id": 99,
        "ticker": "XLF",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "target_price": 120.0,
        # entry_time deliberately absent
    }
    signal = strategy.evaluate_exit(position=pos, current_price=100.5)
    # Should return False (no exit) without raising
    assert signal.should_exit is False


@pytest.mark.unit
def test_time_stop_skipped_when_entry_time_malformed() -> None:
    """Malformed entry_time string silently skips the time_stop."""
    strategy = _strategy({"max_hold_days": 1})
    pos: dict[str, Any] = {
        "id": 99,
        "ticker": "XLF",
        "entry_time": "not-a-real-datetime",
        "entry_price": 100.0,
        "stop_price": 90.0,
        "target_price": 120.0,
    }
    signal = strategy.evaluate_exit(position=pos, current_price=100.5)
    assert signal.should_exit is False


# ---------------------------------------------------------------------------
# G. Weekend days not counted as trading days
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_weekend_not_counted_as_trading_day() -> None:
    """A Thu→Mon span is 1 trading day (Fri–Sun = 3 calendar days, 0 trading)."""
    strategy = _strategy({"max_hold_days": 2})
    # Entry Thu at close; 'now' is Mon morning — only 1 trading day (Mon) elapsed
    entry = datetime(2026, 5, 21, 15, 45, 0, tzinfo=ET)  # Thursday
    now = datetime(2026, 5, 25, 9, 35, 0, tzinfo=ET)     # Monday

    pos = _position(entry_time=entry, stop_price=90.0, target_price=120.0)

    with patch("trading_bot.strategy.strategies.mean_reversion.datetime") as mock_dt:
        mock_dt.fromisoformat.side_effect = datetime.fromisoformat
        mock_dt.now.return_value = now

        signal = strategy.evaluate_exit(
            position=pos,
            current_price=100.5,
        )

    # 1 trading day < max_hold_days=2 → no exit
    assert signal.should_exit is False

"""Tests for MultiStrategyBacktester._resolve_intraday_exits.

Covers the --honor-strategy-exits mode that lets a sleeve's live exit config
(its own stop/target/trail) be backtested faithfully instead of the ATR
override. See docs/research/orb_exit_mismatch_2026-06-03.md.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from trading_bot.multi_strategy_backtest import MultiStrategyBacktester

resolve = MultiStrategyBacktester._resolve_intraday_exits

# ATR reference values (entry 100): stop 96, target 110, trail 0.05, act 0.035.
ATR = dict(atr_stop=96.0, atr_target=110.0, atr_trail=0.05, atr_activation=0.035)
FILL = 100.0


@dataclass
class _Decision:
    stop_price: float | None = 98.0
    target_price: float | None = 104.0
    trail_pct: float | None = 0.02


# --- honor=False: historical ATR override -----------------------------------


@pytest.mark.unit
def test_atr_override_uses_atr_when_target_set():
    stop, target, trail, act = resolve(_Decision(), FILL, honor=False, **ATR)
    assert (stop, target, trail) == (96.0, 110.0, 0.05)  # all ATR
    assert act == 0.035


@pytest.mark.unit
def test_atr_override_let_winners_run_when_target_none():
    stop, target, trail, act = resolve(
        _Decision(target_price=None), FILL, honor=False, **ATR
    )
    assert target is None  # ride the trail, no fixed target
    assert stop == 96.0 and trail == 0.05
    assert act == pytest.approx((110.0 - 100.0) / 100.0)  # 0.10 from ATR target


# --- honor=True: strategy's own exits ---------------------------------------


@pytest.mark.unit
def test_honor_uses_strategy_stop_target_trail():
    stop, target, trail, act = resolve(_Decision(), FILL, honor=True, **ATR)
    assert (stop, target, trail) == (98.0, 104.0, 0.02)  # all from the decision
    assert act == 0.035


@pytest.mark.unit
def test_honor_target_none_lets_winners_run():
    stop, target, trail, act = resolve(
        _Decision(target_price=None), FILL, honor=True, **ATR
    )
    assert target is None
    assert stop == 98.0 and trail == 0.02  # still the strategy's stop/trail
    assert act == pytest.approx(0.10)


@pytest.mark.unit
def test_honor_falls_back_to_atr_for_unset_stop():
    stop, _, _, _ = resolve(_Decision(stop_price=0.0), FILL, honor=True, **ATR)
    assert stop == 96.0  # 0 stop -> ATR fallback


@pytest.mark.unit
def test_honor_falls_back_to_atr_for_unset_trail():
    _, _, trail, _ = resolve(_Decision(trail_pct=None), FILL, honor=True, **ATR)
    assert trail == 0.05  # None trail -> ATR fallback

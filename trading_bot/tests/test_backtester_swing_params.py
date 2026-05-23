"""Tests for the backtester's SWING-decision honor path.

Covers ``MultiStrategyBacktester._resolve_swing_decision_params`` plus
the ``_check_trade_exit`` position-dict surface that cross-sectional
strategies rely on for rebalance-out exits.
"""

from __future__ import annotations

import pytest

from trading_bot.constants import HoldType
from trading_bot.strategy.base import StrategyDecision
from trading_bot.multi_strategy_backtest import MultiStrategyBacktester


def _decision(
    hold_type: HoldType = HoldType.SWING,
    stop_price: float = 85.0,
    shares: float = 5.0,
    target_price: float | None = None,
    trail_pct: float | None = None,
) -> StrategyDecision:
    return StrategyDecision(
        ticker="XLK",
        exchange="NYSE",
        direction="long",
        shares=shares,
        entry_price=100.0,
        stop_price=stop_price,
        target_price=target_price,
        trail_pct=trail_pct,
        hold_type=hold_type,
        strategy_id="test_strategy",
    )


@pytest.fixture
def backtester() -> MultiStrategyBacktester:
    """A minimally-initialized backtester instance.

    We don't go through __init__ (which requires config + enabled
    strategies). The unit test only exercises pure-function logic on
    ``_resolve_swing_decision_params`` so we can short-circuit the
    constructor by allocating an object directly.
    """
    bt = MultiStrategyBacktester.__new__(MultiStrategyBacktester)
    return bt


def test_intraday_decision_returns_none(backtester: MultiStrategyBacktester) -> None:
    """INTRADAY decisions fall through to the ATR-override path."""
    dec = _decision(hold_type=HoldType.INTRADAY)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=2500.0, fractional=True,
    )
    assert result is None


def test_swing_without_stop_returns_none(backtester: MultiStrategyBacktester) -> None:
    """SWING but stop_price == 0 falls through to ATR path."""
    dec = _decision(stop_price=0.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=2500.0, fractional=True,
    )
    assert result is None


def test_swing_without_shares_returns_none(backtester: MultiStrategyBacktester) -> None:
    """SWING but shares == 0 falls through to ATR path."""
    dec = _decision(shares=0.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=2500.0, fractional=True,
    )
    assert result is None


def test_swing_with_explicit_params_honored(backtester: MultiStrategyBacktester) -> None:
    """SWING + stop_price + shares → strategy params used as-is."""
    dec = _decision(
        stop_price=85.0, shares=5.0,
        target_price=120.0, trail_pct=0.05,
    )
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=2500.0, fractional=True,
    )
    assert result is not None
    stop, target, trail, shares = result
    assert stop == 85.0
    assert target == 120.0
    assert trail == 0.05
    assert shares == pytest.approx(5.0)


def test_swing_target_and_trail_can_be_none(backtester: MultiStrategyBacktester) -> None:
    """Returning None for target/trail is preserved (no fabricated values)."""
    dec = _decision(stop_price=85.0, shares=5.0, target_price=None, trail_pct=None)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=2500.0, fractional=True,
    )
    assert result is not None
    _, target, trail, _ = result
    assert target is None
    assert trail is None


def test_shares_capped_by_available_cash_fractional(
    backtester: MultiStrategyBacktester,
) -> None:
    """If decision.shares would exceed what cash can afford, shares are
    truncated to the affordable amount (rounded to 4dp for fractional)."""
    # cash 500 / price 100 = 5 affordable; decision asks for 10
    dec = _decision(stop_price=85.0, shares=10.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=500.0, fractional=True,
    )
    assert result is not None
    _, _, _, shares = result
    assert shares == pytest.approx(5.0)


def test_shares_capped_integer_when_non_fractional(
    backtester: MultiStrategyBacktester,
) -> None:
    """Integer mode: 250 / 100 = 2.5 → floor to 2 affordable shares."""
    dec = _decision(stop_price=85.0, shares=10.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=250.0, fractional=False,
    )
    assert result is not None
    _, _, _, shares = result
    assert shares == 2.0


def test_returns_none_when_truncation_falls_below_min(
    backtester: MultiStrategyBacktester,
) -> None:
    """If cash is insufficient even for the fractional floor, skip."""
    # cash 0.05 / price 100 = 0.0005 < 0.001 fractional floor
    dec = _decision(stop_price=85.0, shares=10.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=100.0, cash_usd=0.05, fractional=True,
    )
    assert result is None


def test_returns_none_when_fill_price_non_positive(
    backtester: MultiStrategyBacktester,
) -> None:
    dec = _decision(stop_price=85.0, shares=5.0)
    result = backtester._resolve_swing_decision_params(
        dec, fill_price=0.0, cash_usd=2500.0, fractional=True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# position_dict surfaces ticker (regression — exits couldn't rebalance-out
# without this, manifest as cross_sectional_momentum holding positions
# forever instead of swapping on monthly rebalance days).
# ---------------------------------------------------------------------------


def test_check_trade_exit_passes_ticker_to_strategy() -> None:
    """The position_dict built by _check_trade_exit must include 'ticker'
    so universe-aware strategies can decide whether the trade is still
    in their top-N (and exit it if not)."""
    from datetime import datetime
    from trading_bot.multi_strategy_backtest import StrategyTrade
    from trading_bot.strategy.base import ExitSignal, StrategyBase

    captured: dict[str, dict] = {}

    class _CaptureStrategy(StrategyBase):
        def __init__(self) -> None:
            super().__init__(
                strategy_id="cap", display_name="Cap",
                config={}, db_path=None,
            )

        def evaluate_entry(self, *a, **k):
            return None

        def evaluate_exit(self, position, current_price, df_5min=None, df_daily=None):
            captured["position"] = position
            return ExitSignal(should_exit=False)

        def get_max_positions(self):
            return 1

    bt = MultiStrategyBacktester.__new__(MultiStrategyBacktester)
    trade = StrategyTrade(
        strategy_id="cap", ticker="XLK", exchange="NYSE",
        entry_time=datetime(2026, 1, 2, 9, 35),
        entry_price=100.0, shares=5.0,
        stop_price=85.0, target_price=None, trail_pct=None,
        signals={}, hold_type="swing", sentiment_score=None,
        trail_activation_pct=0.0, highest_price=100.0,
    )
    result = bt._check_trade_exit(
        trade, bar_close=99.0, bar_high=100.5, bar_low=98.5,
        current_time=datetime(2026, 1, 2, 9, 40),
        strategy=_CaptureStrategy(),
        df_5min=None, df_daily=None,
    )
    assert result is None  # strategy returned no-exit
    assert "ticker" in captured["position"]
    assert captured["position"]["ticker"] == "XLK"

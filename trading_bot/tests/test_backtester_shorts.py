"""Direction-aware backtester tests (PR1 for gap-fill sleeve #48).

The multi-strategy backtester was long-only: stops fired on ``bar_low``,
targets on ``bar_high``, and P&L was ``(exit - entry) * shares``. The
opening gap-fill sleeve needs to *short* gap-ups, so the engine's exit
checks, close accounting, and the intraday ``run`` entry path must become
direction-aware.

These tests pin the new short behaviour AND guard that the long path is
unchanged (the regression bar for this PR). Long-path coverage also lives
in ``test_multi_strategy_backtest.py``; the explicit long asserts here are
deliberate belt-and-braces around the direction branch.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.config import Config
from trading_bot.constants import HoldType
from trading_bot.multi_strategy_backtest import (
    MultiStrategyBacktester,
    StrategyTrade,
    _StrategyState,
)
from trading_bot.strategy.base import ExitSignal, StrategyDecision

TZ_ET = ZoneInfo("US/Eastern")


@pytest.fixture
def config() -> Config:
    return Config.load("config.yaml")


def _make_strategy_mock(strategy_id: str = "gap_fill") -> MagicMock:
    mock = MagicMock()
    mock.strategy_id = strategy_id
    mock.display_name = "Gap Fill (test)"
    mock.get_max_positions.return_value = 1
    mock.evaluate_entry.return_value = None
    mock.evaluate_exit.return_value = ExitSignal(should_exit=False)
    return mock


def _short_trade(
    *,
    entry_price: float = 100.0,
    shares: float = 10.0,
    stop_price: float = 102.0,
    target_price: float | None = 97.0,
    trail_pct: float | None = None,
    lowest_price: float | None = None,
) -> StrategyTrade:
    now = datetime.now(TZ_ET)
    return StrategyTrade(
        strategy_id="gap_fill",
        ticker="SPY",
        exchange="NYSE",
        entry_time=now - timedelta(hours=1),
        entry_price=entry_price,
        shares=shares,
        stop_price=stop_price,
        target_price=target_price,
        trail_pct=trail_pct,
        signals={},
        hold_type="intraday",
        direction="short",
        lowest_price=lowest_price if lowest_price is not None else entry_price,
    )


def _long_trade(
    *,
    entry_price: float = 100.0,
    shares: float = 10.0,
    stop_price: float = 98.0,
    target_price: float | None = 103.0,
) -> StrategyTrade:
    now = datetime.now(TZ_ET)
    return StrategyTrade(
        strategy_id="gap_fill",
        ticker="SPY",
        exchange="NYSE",
        entry_time=now - timedelta(hours=1),
        entry_price=entry_price,
        shares=shares,
        stop_price=stop_price,
        target_price=target_price,
        trail_pct=None,
        signals={},
        hold_type="intraday",
        direction="long",
        highest_price=entry_price,
    )


# ---------------------------------------------------------------------------
# StrategyTrade: new direction field
# ---------------------------------------------------------------------------

class TestTradeDirectionField:
    def test_direction_defaults_to_long(self) -> None:
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="t", ticker="SPY", exchange="NYSE",
            entry_time=now, entry_price=100.0, shares=10,
            stop_price=98.0, target_price=103.0, trail_pct=None, signals={},
        )
        assert trade.direction == "long"

    def test_direction_can_be_short(self) -> None:
        assert _short_trade().direction == "short"


# ---------------------------------------------------------------------------
# Short exit checking — stops fire on the upside, targets on the downside
# ---------------------------------------------------------------------------

class TestShortExitChecking:
    def test_short_stop_fires_on_bar_high(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _short_trade(entry_price=100.0, stop_price=102.0, target_price=97.0)
        # Price spikes up: bar_high 102.5 breaches the 102 stop.
        result = engine._check_trade_exit(
            trade, bar_close=102.2, bar_high=102.5, bar_low=101.0,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is not None
        reason, px = result
        assert reason == "stop_loss"
        # Cover fills at the stop or worse (higher).
        assert px >= 102.0

    def test_short_target_fires_on_bar_low(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _short_trade(entry_price=100.0, stop_price=102.0, target_price=97.0)
        # Price drops: bar_low 96.5 reaches the 97 target.
        result = engine._check_trade_exit(
            trade, bar_close=96.8, bar_high=98.0, bar_low=96.5,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is not None
        reason, px = result
        assert reason == "take_profit"
        assert px == 97.0

    def test_short_no_exit_when_in_range(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _short_trade(entry_price=100.0, stop_price=102.0, target_price=97.0)
        result = engine._check_trade_exit(
            trade, bar_close=99.5, bar_high=100.5, bar_low=98.5,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is None

    def test_short_stop_not_triggered_by_bar_low(self, config: Config) -> None:
        """A short must NOT stop out because price fell (that's profit).

        Regression guard against accidentally reusing the long
        ``bar_low <= stop`` rule for shorts.
        """
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        # No target so only the stop could fire; price falls hard.
        trade = _short_trade(entry_price=100.0, stop_price=102.0, target_price=None)
        result = engine._check_trade_exit(
            trade, bar_close=95.0, bar_high=99.0, bar_low=94.0,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is None

    def test_short_trailing_stop_fires_on_rebound(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        # Short from 100, price fell to a low of 90 (lowest_price), trail 4%.
        # Trail stop = 90 * 1.04 = 93.6; a rebound bar_high 94 breaches it.
        trade = _short_trade(
            entry_price=100.0, stop_price=110.0, target_price=None,
            trail_pct=0.04, lowest_price=90.0,
        )
        result = engine._check_trade_exit(
            trade, bar_close=93.8, bar_high=94.0, bar_low=92.5,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is not None
        assert result[0] == "trailing_stop"

    def test_short_lowest_price_tracked(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _short_trade(entry_price=100.0, lowest_price=100.0, target_price=None,
                             stop_price=110.0)
        engine._check_trade_exit(
            trade, bar_close=96.0, bar_high=97.0, bar_low=95.0,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert trade.lowest_price == 95.0


# ---------------------------------------------------------------------------
# Short close accounting — P&L is (entry - exit) * shares; cover spends cash
# ---------------------------------------------------------------------------

class TestShortCloseTrade:
    def test_short_winning_close_is_positive_pnl(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=2000.0, initial_cash_usd=1000.0)
        trade = _short_trade(entry_price=100.0, shares=10.0, target_price=97.0)
        state.open_positions.append(trade)
        now = datetime.now(TZ_ET)
        engine._close_trade(state, trade, exit_price=97.0, reason="take_profit", exit_time=now)

        assert trade in state.closed_trades
        assert trade not in state.open_positions
        assert trade.net_pnl_usd > 0
        assert state.wins == 1
        assert state.losses == 0

    def test_short_losing_close_is_negative_pnl(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=2000.0, initial_cash_usd=1000.0)
        trade = _short_trade(entry_price=100.0, shares=10.0, stop_price=102.0)
        state.open_positions.append(trade)
        now = datetime.now(TZ_ET)
        engine._close_trade(state, trade, exit_price=102.0, reason="stop_loss", exit_time=now)

        assert trade.net_pnl_usd < 0
        assert state.losses == 1

    def test_short_close_spends_cash_to_cover(self, config: Config) -> None:
        """Covering a short BUYS shares back → cash decreases by the fill cost."""
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=2000.0, initial_cash_usd=1000.0)
        trade = _short_trade(entry_price=100.0, shares=10.0, target_price=97.0)
        state.open_positions.append(trade)
        now = datetime.now(TZ_ET)
        cash_before = state.cash_usd
        engine._close_trade(state, trade, exit_price=97.0, reason="take_profit", exit_time=now)
        # Cover fill = 97 + slippage; cash drops by fill * shares.
        assert trade.exit_price is not None
        assert state.cash_usd == pytest.approx(cash_before - trade.exit_price * trade.shares)

    def test_short_cover_fill_has_buy_slippage(self, config: Config) -> None:
        """The cover leg is a BUY, so it fills *above* the requested price."""
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=2000.0, initial_cash_usd=1000.0)
        trade = _short_trade(entry_price=100.0, shares=10.0, target_price=97.0)
        state.open_positions.append(trade)
        now = datetime.now(TZ_ET)
        engine._close_trade(state, trade, exit_price=97.0, reason="take_profit", exit_time=now)
        assert trade.exit_price is not None
        assert trade.exit_price > 97.0


# ---------------------------------------------------------------------------
# Long path regression — direction branch must not alter long behaviour
# ---------------------------------------------------------------------------

class TestLongPathUnchanged:
    def test_long_stop_fires_on_bar_low(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _long_trade(entry_price=100.0, stop_price=98.0, target_price=103.0)
        result = engine._check_trade_exit(
            trade, bar_close=97.8, bar_high=99.0, bar_low=97.5,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is not None
        assert result[0] == "stop_loss"
        assert result[1] <= 98.0

    def test_long_target_fires_on_bar_high(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = _long_trade(entry_price=100.0, stop_price=98.0, target_price=103.0)
        result = engine._check_trade_exit(
            trade, bar_close=103.2, bar_high=103.5, bar_low=102.0,
            current_time=now, strategy=strat,
            df_5min=pd.DataFrame(), df_daily=pd.DataFrame(),
        )
        assert result is not None
        assert result[0] == "take_profit"
        assert result[1] == 103.0

    def test_long_close_adds_proceeds_to_cash(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=900.0, initial_cash_usd=1000.0)
        trade = _long_trade(entry_price=100.0, shares=10.0, target_price=103.0)
        state.open_positions.append(trade)
        now = datetime.now(TZ_ET)
        cash_before = state.cash_usd
        engine._close_trade(state, trade, exit_price=103.0, reason="take_profit", exit_time=now)
        assert trade.exit_price is not None
        # Long exit SELLS → cash increases by sell-fill proceeds.
        assert state.cash_usd == pytest.approx(cash_before + trade.exit_price * trade.shares)
        assert trade.net_pnl_usd > 0
        assert state.wins == 1


# ---------------------------------------------------------------------------
# End-to-end: a profitable short through the intraday ``run`` loop
# ---------------------------------------------------------------------------

def _declining_5min_df(start_date: date) -> pd.DataFrame:
    """80 declining 5-min bars from 09:30 ET so a short fills its target."""
    start_dt = datetime(start_date.year, start_date.month, start_date.day,
                        9, 30, 0, tzinfo=TZ_ET)
    n = 80
    timestamps = [start_dt + timedelta(minutes=5 * i) for i in range(n)]
    prices = [100.0 - 0.25 * i for i in range(n)]  # 100 → 80.25
    data = {
        "open": [p + 0.05 for p in prices],
        "high": [p + 0.10 for p in prices],
        "low": [p - 0.10 for p in prices],
        "close": prices,
        "volume": [100000] * n,
    }
    return pd.DataFrame(
        data, index=pd.DatetimeIndex(timestamps, tz=TZ_ET, name="timestamp")
    )


class TestEndToEndShort:
    @pytest.mark.asyncio
    async def test_profitable_short_increases_cash(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)

        df_5min = _declining_5min_df(date(2026, 3, 16))
        mock_data = {"SPY": {"intraday": df_5min, "daily": pd.DataFrame()}}

        # One strategy that shorts SPY once, on bar index 12, targeting a
        # lower price the declining series is guaranteed to reach.
        strat = engine._strategies[0]
        fired: dict[str, bool] = {"done": False}

        def _entry(*args: Any, **kwargs: Any) -> StrategyDecision | None:
            if fired["done"]:
                return None
            price = float(kwargs["current_price"])
            # Only fire once we're a few bars in (the run loop skips idx<10).
            if price > 97.0:
                return None
            fired["done"] = True
            return StrategyDecision(
                ticker="SPY", exchange="NYSE", direction="short",
                shares=10.0, entry_price=price,
                stop_price=price + 3.0, target_price=price - 5.0,
                trail_pct=None, hold_type=HoldType.INTRADAY,
                strategy_id=strat.strategy_id,
            )

        with patch.object(strat, "evaluate_entry", side_effect=_entry), \
             patch.object(strat, "evaluate_exit", return_value=ExitSignal(should_exit=False)), \
             patch.object(strat, "get_max_positions", return_value=1), \
             patch.object(engine, "_strategies", [strat]), \
             patch.object(engine, "_load_day_data", return_value=mock_data):
            result = await engine.run(
                date(2026, 3, 16), date(2026, 3, 16), cash_per_strategy_usd=1000.0,
            )

        sr = result.strategies[0]
        assert sr.total_trades == 1, "short entry should have opened exactly one trade"
        assert sr.final_cash_usd > sr.initial_cash_usd, (
            "a short in a falling market must be profitable"
        )
        assert sr.wins == 1

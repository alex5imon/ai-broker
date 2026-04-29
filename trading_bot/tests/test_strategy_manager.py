"""Tests for StrategyManager — the multi-strategy orchestrator."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from trading_bot.constants import HoldType
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.strategy_manager import StrategyManager


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubStrategy(StrategyBase):
    """Minimal strategy stub: returns a canned entry/exit decision."""

    def __init__(
        self,
        strategy_id: str = "stub",
        decision: StrategyDecision | None = None,
        exit_signal: ExitSignal | None = None,
        max_positions: int = 2,
    ) -> None:
        super().__init__(strategy_id=strategy_id, display_name=strategy_id, config={})
        self._decision = decision
        self._exit_signal = exit_signal or ExitSignal(should_exit=False)
        self._max_positions = max_positions
        self.evaluate_entry_calls: list[tuple[str, float]] = []
        self.evaluate_exit_calls: list[tuple[str, float]] = []

    def evaluate_entry(
        self,
        ticker: str,
        exchange: str,
        df_5min: pd.DataFrame,
        df_daily: pd.DataFrame,
        current_price: float,
        available_cash: float,
        sentiment_score: float | None = None,
    ) -> StrategyDecision | None:
        self.evaluate_entry_calls.append((ticker, current_price))
        if self._decision is None:
            return None
        # Return a copy with the requested ticker so the manager wires it through.
        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction=self._decision.direction,
            shares=self._decision.shares,
            entry_price=current_price,
            stop_price=current_price * 0.98,
            target_price=current_price * 1.04,
            trail_pct=self._decision.trail_pct,
            hold_type=self._decision.hold_type,
            strategy_id=self.strategy_id,
            signals=self._decision.signals,
            sentiment_score=sentiment_score,
            trail_activation_price=self._decision.trail_activation_price,
        )

    def evaluate_exit(
        self,
        position: dict[str, Any],
        current_price: float,
        df_5min: pd.DataFrame | None = None,
        df_daily: pd.DataFrame | None = None,
    ) -> ExitSignal:
        self.evaluate_exit_calls.append((position["ticker"], current_price))
        return self._exit_signal

    def get_max_positions(self) -> int:
        return self._max_positions


def _bars(n: int = 30, start: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [start] * n,
            "high": [start * 1.01] * n,
            "low": [start * 0.99] * n,
            "close": [start] * n,
            "volume": [1_000_000] * n,
        }
    )


@pytest.fixture
def fake_5min():
    async def _f(ticker: str, exchange: str) -> pd.DataFrame:
        return _bars()
    return _f


@pytest.fixture
def fake_daily():
    async def _f(ticker: str, exchange: str) -> pd.DataFrame:
        return _bars(60)
    return _f


@pytest.fixture
def base_market_data():
    md = MagicMock()
    md.trading_paused = False
    md.get_latest_price = MagicMock(return_value=100.0)
    md.is_stale = MagicMock(return_value=False)
    return md


@pytest.fixture
def base_risk_manager():
    rm = MagicMock()
    rm.can_trade = MagicMock(return_value=(True, "ok"))
    return rm


@pytest.fixture
def base_earnings():
    ec = MagicMock()
    ec.is_in_blackout = MagicMock(return_value=False)
    return ec


@pytest.fixture
def base_sentiment():
    sa = MagicMock()
    sa.get_sentiment = AsyncMock(return_value=0.2)
    return sa


@pytest.fixture
def base_order_manager():
    om = MagicMock()
    om.place_entry = AsyncMock(return_value=42)
    om.place_exit = AsyncMock(return_value="alpaca-exit-1")
    return om


@pytest.fixture
def base_portfolio_manager():
    pm = MagicMock()
    portfolio = MagicMock()
    portfolio.get_open_positions = MagicMock(return_value=[])
    portfolio.available_cash = 1000.0
    portfolio.record_entry = MagicMock()
    portfolio.record_exit = MagicMock()
    pm.get_portfolio = MagicMock(return_value=portfolio)
    pm._portfolio = portfolio  # for assertions
    return pm


@pytest.fixture
def base_config():
    cfg = MagicMock()
    phase = MagicMock()
    phase.value = 1
    cfg.get_phase = MagicMock(return_value=phase)
    return cfg


def _make_decision() -> StrategyDecision:
    return StrategyDecision(
        ticker="SPY",
        exchange="US",
        direction="long",
        shares=10,
        entry_price=100.0,
        stop_price=98.0,
        target_price=104.0,
        trail_pct=0.02,
        hold_type=HoldType.INTRADAY,
        strategy_id="stub",
        signals={"why": "test"},
        sentiment_score=0.2,
        trail_activation_price=102.0,
    )


# ---------------------------------------------------------------------------
# scan_for_entries
# ---------------------------------------------------------------------------


class TestScanForEntries:
    @pytest.mark.asyncio
    async def test_market_data_paused_short_circuits(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        base_market_data.trading_paused = True
        sm = StrategyManager(
            strategies=[_StubStrategy(decision=_make_decision())],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        base_order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_regime_filter_blocks_entries(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        regime = MagicMock()
        regime.allows_new_entries = AsyncMock(return_value=False)

        sm = StrategyManager(
            strategies=[_StubStrategy(decision=_make_decision())],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
            regime_filter=regime,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        base_order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_regime_filter_failure_does_not_block(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        regime = MagicMock()
        regime.allows_new_entries = AsyncMock(side_effect=RuntimeError("boom"))

        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
            regime_filter=regime,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 1
        base_order_manager.place_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_risk_block_stops_loop(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        base_risk_manager.can_trade = MagicMock(return_value=(False, "kill_switch"))
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY", "QQQ"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        assert strategy.evaluate_entry_calls == []

    @pytest.mark.asyncio
    async def test_stale_data_skips_ticker(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        base_market_data.is_stale = MagicMock(side_effect=lambda t: t == "SPY")
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        await sm.scan_for_entries(
            watchlist=["SPY", "QQQ"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        # Only QQQ evaluated (SPY skipped as stale)
        assert [t for t, _ in strategy.evaluate_entry_calls] == ["QQQ"]

    @pytest.mark.asyncio
    async def test_earnings_blackout_skips_ticker(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        base_earnings.is_in_blackout = MagicMock(side_effect=lambda t, _: t == "SPY")
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        await sm.scan_for_entries(
            watchlist=["SPY", "QQQ"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert [t for t, _ in strategy.evaluate_entry_calls] == ["QQQ"]

    @pytest.mark.asyncio
    async def test_max_positions_blocks_entry(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        portfolio = base_portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(
            return_value=[{"ticker": "QQQ"}, {"ticker": "AAPL"}]
        )
        strategy = _StubStrategy(decision=_make_decision(), max_positions=2)
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0

    @pytest.mark.asyncio
    async def test_double_entry_same_ticker_blocked(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        portfolio = base_portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=[{"ticker": "SPY"}])
        strategy = _StubStrategy(decision=_make_decision(), max_positions=5)
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        base_order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_dry_run_does_not_place_orders(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
            dry_run=True,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        base_order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_places_entry(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        portfolio = base_portfolio_manager._portfolio
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 1
        base_order_manager.place_entry.assert_called_once()
        portfolio.record_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_strategy_evaluation_error_swallowed(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        bad = _StubStrategy(strategy_id="bad")
        bad.evaluate_entry = MagicMock(side_effect=RuntimeError("kaboom"))  # type: ignore[method-assign]
        good = _StubStrategy(strategy_id="good", decision=_make_decision())

        sm = StrategyManager(
            strategies=[bad, good],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        # The good strategy still places its entry despite bad raising.
        assert n == 1

    @pytest.mark.asyncio
    async def test_falls_back_to_5min_close_when_latest_price_missing(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_daily,
        tmp_db_path,
    ):
        base_market_data.get_latest_price = MagicMock(return_value=None)

        async def fetch_5min(ticker: str, exchange: str) -> pd.DataFrame:
            return _bars(start=123.0)

        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fetch_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        # Fallback worked, entry placed
        assert n == 1
        # Strategy got the fallback price (123.0)
        assert strategy.evaluate_entry_calls[0][1] == 123.0

    @pytest.mark.asyncio
    async def test_bar_fetch_failure_skips_ticker(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_daily,
        tmp_db_path,
    ):
        async def boom(ticker: str, exchange: str) -> pd.DataFrame:
            raise RuntimeError("network down")

        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=boom,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 0
        assert strategy.evaluate_entry_calls == []

    @pytest.mark.asyncio
    async def test_sentiment_failure_does_not_block(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        fake_5min,
        fake_daily,
        tmp_db_path,
    ):
        sentiment = MagicMock()
        sentiment.get_sentiment = AsyncMock(side_effect=RuntimeError("403"))
        strategy = _StubStrategy(decision=_make_decision())
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.scan_for_entries(
            watchlist=["SPY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        assert n == 1


# ---------------------------------------------------------------------------
# check_exits
# ---------------------------------------------------------------------------


class TestCheckExits:
    def _setup(
        self,
        market_data,
        portfolio_manager,
        order_manager,
        config,
        risk_manager,
        sentiment,
        earnings,
        db_path: str,
        positions: list[dict[str, Any]],
        exit_signal: ExitSignal,
        dry_run: bool = False,
    ) -> tuple[StrategyManager, _StubStrategy, MagicMock]:
        portfolio = portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=positions)
        strategy = _StubStrategy(decision=None, exit_signal=exit_signal)
        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=portfolio_manager,
            market_data=market_data,
            order_manager=order_manager,
            risk_manager=risk_manager,
            sentiment=sentiment,
            earnings=earnings,
            config=config,
            db_path=db_path,
            dry_run=dry_run,
        )
        return sm, strategy, portfolio

    @pytest.mark.asyncio
    async def test_no_exit_signal(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
    ):
        sm, _, portfolio = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{"ticker": "SPY", "entry_price": 100.0, "quantity": 5}],
            exit_signal=ExitSignal(should_exit=False),
        )
        n = await sm.check_exits()
        assert n == 0
        portfolio.record_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_exit_signal_records_exit(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
    ):
        sm, _, portfolio = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{"ticker": "SPY", "entry_price": 100.0, "quantity": 5}],
            exit_signal=ExitSignal(should_exit=True, reason="take_profit"),
        )
        n = await sm.check_exits()
        assert n == 1
        portfolio.record_exit.assert_called_once_with(5, 100.0, 100.0)

    @pytest.mark.asyncio
    async def test_dry_run_no_exit_recorded(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
    ):
        sm, _, portfolio = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{"ticker": "SPY", "entry_price": 100.0, "quantity": 5}],
            exit_signal=ExitSignal(should_exit=True, reason="stop_loss"),
            dry_run=True,
        )
        n = await sm.check_exits()
        assert n == 0
        portfolio.record_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_price_skips(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
    ):
        base_market_data.get_latest_price = MagicMock(return_value=None)
        sm, strategy, _ = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{"ticker": "SPY", "entry_price": 100.0, "quantity": 5}],
            exit_signal=ExitSignal(should_exit=True, reason="stop_loss"),
        )
        n = await sm.check_exits()
        assert n == 0
        assert strategy.evaluate_exit_calls == []


def test_get_comparison_report(
    base_market_data,
    base_risk_manager,
    base_earnings,
    base_sentiment,
    base_order_manager,
    base_portfolio_manager,
    base_config,
    tmp_db_path,
):
    base_portfolio_manager.get_comparison_report = MagicMock(return_value={"k": {"v": 1}})
    sm = StrategyManager(
        strategies=[_StubStrategy()],
        portfolio_manager=base_portfolio_manager,
        market_data=base_market_data,
        order_manager=base_order_manager,
        risk_manager=base_risk_manager,
        sentiment=base_sentiment,
        earnings=base_earnings,
        config=base_config,
        db_path=tmp_db_path,
    )
    assert sm.get_comparison_report() == {"k": {"v": 1}}
    assert sm.strategies[0].strategy_id == "stub"

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
    # Default Alpaca state for drain checks: position IS held same-side as
    # whatever the test seeds. Tests that want NOT_HELD or OPPOSITE_SIDE
    # override this attribute.
    om._gw = MagicMock()
    pos = MagicMock()
    pos.qty = "1.0"
    om._gw.client.get_open_position = MagicMock(return_value=pos)
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
# Drain disabled sleeves (2026-04-29 incident)
# ---------------------------------------------------------------------------


class TestDrainDisabledSleeves:
    """Positions tagged with a now-disabled strategy must be flushed."""

    @pytest.mark.asyncio
    async def test_drains_position_for_disabled_strategy(
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
        # Seed positions: one for an ACTIVE strategy, one for a DISABLED
        # strategy. Only the disabled one should be drained.
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (?, 'US', 'USD', 5.0, 100.0, ?, 'STOP_ACTIVE', 'swing', 1, ?)
            """,
            ("SPY", "2026-04-27T15:40:00-04:00", "breakout"),
        )
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (?, 'US', 'USD', 1.0, 100.0, ?, 'POSITION_OPEN', 'swing', 1, ?)
            """,
            ("XLY", "2026-04-29T10:00:00-04:00", "stub"),  # active
        )
        conn.commit()
        conn.close()

        sm = StrategyManager(
            strategies=[_StubStrategy(decision=_make_decision())],  # only "stub" active
            portfolio_manager=base_portfolio_manager,
            market_data=base_market_data,
            order_manager=base_order_manager,
            risk_manager=base_risk_manager,
            sentiment=base_sentiment,
            earnings=base_earnings,
            config=base_config,
            db_path=tmp_db_path,
        )

        n = await sm.drain_disabled_sleeves()
        assert n == 1, f"expected 1 drained, got {n}"

        # Exit was placed on the orphan ticker, NOT the active one.
        base_order_manager.place_exit.assert_awaited_once()
        call = base_order_manager.place_exit.await_args
        assert call.kwargs["ticker"] == "SPY"
        assert "orphan_sleeve_drain" in call.kwargs["reason"]
        assert "breakout" in call.kwargs["reason"]

    @pytest.mark.asyncio
    async def test_drains_unknown_strategy_id(
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
        """Untagged ('unknown') legacy positions should be drained too."""
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('QQQ', 'US', 'USD', 1.0, 100.0, ?, 'POSITION_OPEN', 'swing', 1, 'unknown')
            """,
            ("2026-04-27T10:26:00-04:00",),
        )
        conn.commit()
        conn.close()

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

        n = await sm.drain_disabled_sleeves()
        assert n == 1
        base_order_manager.place_exit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drain_noop_when_all_active(
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
        """No DB rows → drain returns 0 and never touches order_manager."""
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
        n = await sm.drain_disabled_sleeves()
        assert n == 0
        base_order_manager.place_exit.assert_not_awaited()

    # -----------------------------------------------------------------
    # Alpaca-side checks (2026-04-30 incident — drain submitted SELLs
    # against an already-flat sleeve and built an unbounded short on
    # the paper account every time the bot ticked).
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_drain_skips_when_alpaca_holds_zero(
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
        """DB says +1 SPY but Alpaca already holds 0 → don't submit a
        drain SELL (it would open a new short). Mark DB CLOSED instead."""
        import sqlite3
        from alpaca.common.exceptions import APIError

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                id, ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (99, 'SPY', 'US', 'USD', 1.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        # Alpaca says "no position". alpaca-py raises APIError; our
        # broad except in _check_alpaca_position catches it.
        base_order_manager._gw.client.get_open_position.side_effect = (
            APIError({"code": 40410000, "message": "position does not exist"})
        )

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

        n = await sm.drain_disabled_sleeves()
        assert n == 0, "should not count a skipped drain as drained"
        base_order_manager.place_exit.assert_not_awaited()

        # DB row is now CLOSED so we don't retry on every subsequent tick.
        # AND a matching trades row is written with exit_reason
        # 'reconciliation_mismatch' and exit_price NULL so the nightly
        # alpaca_backfill can find and repair it. Without this row the
        # P&L for this position would be silently dropped from
        # daily_summaries forever.
        conn = sqlite3.connect(tmp_db_path)
        try:
            pos_row = conn.execute(
                "SELECT status FROM positions WHERE id = 99"
            ).fetchone()
            trade_row = conn.execute(
                "SELECT exit_reason, exit_price, exit_time, ticker, "
                "strategy_id FROM trades "
                "WHERE ticker = 'SPY' AND exit_reason = 'reconciliation_mismatch' "
                "AND exit_price IS NULL"
            ).fetchone()
        finally:
            conn.close()
        assert pos_row is not None
        assert pos_row[0] == "CLOSED"
        assert trade_row is not None, (
            "Drain NOT_HELD must write a trades row for backfill to repair"
        )
        assert trade_row[0] == "reconciliation_mismatch"
        assert trade_row[1] is None
        assert trade_row[2] is not None  # exit_time stamped
        assert trade_row[3] == "SPY"
        assert trade_row[4] == "breakout"

    @pytest.mark.asyncio
    async def test_drain_refuses_when_alpaca_holds_opposite_side(
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
        """DB says +1 SPY, Alpaca says -1 SPY → REFUSE. Submitting a drain
        SELL here would deepen the short to -2. Position stays in DB so
        the operator can investigate (no auto-CLOSE)."""
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                id, ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (77, 'SPY', 'US', 'USD', 1.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        # Alpaca holds OPPOSITE side: -1
        opposite_pos = MagicMock()
        opposite_pos.qty = "-1.0"
        base_order_manager._gw.client.get_open_position.return_value = opposite_pos

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

        n = await sm.drain_disabled_sleeves()
        assert n == 0
        base_order_manager.place_exit.assert_not_awaited()

        # Position NOT marked CLOSED — opposite-side drift needs human eyes.
        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = 77"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "STOP_ACTIVE"

    @pytest.mark.asyncio
    async def test_drain_proceeds_when_alpaca_confirms_same_side(
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
        """DB says +5 SPY, Alpaca confirms +5 → drain SELL proceeds.
        Same as the original drain test but with the Alpaca check now
        explicitly verified."""
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('SPY', 'US', 'USD', 5.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        same_side = MagicMock()
        same_side.qty = "5.0"
        base_order_manager._gw.client.get_open_position.return_value = same_side

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

        n = await sm.drain_disabled_sleeves()
        assert n == 1
        base_order_manager.place_exit.assert_awaited_once()
        # Pin the qty: when DB and Alpaca agree, drain SELLs the full
        # DB qty. The other tests below pin the partial / overshoot
        # cases.
        kwargs = base_order_manager.place_exit.await_args.kwargs
        assert kwargs["qty"] == pytest.approx(5.0)

    # -----------------------------------------------------------------
    # Drain SELL qty bounded by Alpaca truth (ai-broker#41)
    # -----------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_drain_partial_when_alpaca_holds_less_than_db(
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
        """DB says +10 SPY, Alpaca holds +5 (e.g. external partial
        flatten landed between the bot's writes and this tick).

        Submitting SELL 10 here would close the long *and* open a
        short for the missing 5 — the latent half of the 2026-04-30
        incident class. The drain SELL must be bounded by Alpaca's
        actual qty.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('SPY', 'US', 'USD', 10.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        partial = MagicMock()
        partial.qty = "5.0"  # Alpaca holds less than DB
        base_order_manager._gw.client.get_open_position.return_value = partial

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

        n = await sm.drain_disabled_sleeves()
        assert n == 1
        base_order_manager.place_exit.assert_awaited_once()
        kwargs = base_order_manager.place_exit.await_args.kwargs
        assert kwargs["qty"] == pytest.approx(5.0), (
            "drain SELL must be bounded by Alpaca qty (5), not DB qty (10), "
            "or it would close +5 and open -5 short"
        )

    @pytest.mark.asyncio
    async def test_drain_caps_at_db_qty_when_alpaca_holds_more(
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
        """DB says +5 SPY, Alpaca holds +10. The +5 the DB tracks is
        ours to drain; the additional +5 belongs to some other code
        path (manual entry, live add-to-position outside the drain
        loop) and is not the drain's to flatten.

        Submit SELL 5, not 10.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('SPY', 'US', 'USD', 5.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        bigger = MagicMock()
        bigger.qty = "10.0"  # Alpaca holds more than DB tracks
        base_order_manager._gw.client.get_open_position.return_value = bigger

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

        n = await sm.drain_disabled_sleeves()
        assert n == 1
        base_order_manager.place_exit.assert_awaited_once()
        kwargs = base_order_manager.place_exit.await_args.kwargs
        assert kwargs["qty"] == pytest.approx(5.0), (
            "drain must not overshoot DB-tracked qty even if Alpaca holds "
            "more — the surplus is not the drain's to flatten"
        )

    @pytest.mark.asyncio
    async def test_drain_rechecks_alpaca_per_position(
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
        """The Alpaca verification must run before every SELL in the
        drain loop, not just before the first one. Pre-PR-#20 the
        drain looped over a stale snapshot and emitted SELLs in
        pairs ~2 minutes apart — the per-iteration check is what
        prevents a partial pre-tick drain from wrecking later
        iterations.

        Seed two orphan positions on different tickers; assert
        get_open_position is called for each.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES
              ('SPY',  'US', 'USD', 5.0, 100.0,
               '2026-04-27T15:40:00-04:00',
               'STOP_ACTIVE', 'swing', 1, 'breakout'),
              ('XLRE', 'US', 'USD', 20.0, 50.0,
               '2026-04-28T09:40:00-04:00',
               'STOP_ACTIVE', 'swing', 1, 'trend_following')
            """
        )
        conn.commit()
        conn.close()

        # Both Alpaca-side positions match DB: SELL fires for each.
        def _get_open_position(ticker: str):
            pos = MagicMock()
            pos.qty = "5.0" if ticker == "SPY" else "20.0"
            return pos

        base_order_manager._gw.client.get_open_position.side_effect = (
            _get_open_position
        )

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

        n = await sm.drain_disabled_sleeves()
        assert n == 2
        # Per-iteration check ran for each of the two orphan tickers.
        assert (
            base_order_manager._gw.client.get_open_position.call_count == 2
        ), "Alpaca position lookup must run once per orphan position"
        # Two SELLs submitted, one per ticker.
        assert base_order_manager.place_exit.await_count == 2
        called_tickers = {
            call.kwargs["ticker"]
            for call in base_order_manager.place_exit.await_args_list
        }
        assert called_tickers == {"SPY", "XLRE"}

    @pytest.mark.asyncio
    async def test_drain_refuses_on_nan_alpaca_qty(
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
        """If Alpaca returns NaN for ``qty`` (schema drift, parser
        oddity), the drain must refuse to submit any SELL.

        ``NaN > 0`` is False, so the side-mismatch check inside
        ``_check_alpaca_position`` returns OPPOSITE_SIDE — the drain
        logs CRITICAL and skips. Pin this behavior so a future loosening
        of the side-mismatch check can't silently route NaN through
        ``min(qty, abs(NaN))`` (which is NaN) into ``place_exit``.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('SPY', 'US', 'USD', 10.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        nan_pos = MagicMock()
        nan_pos.qty = "nan"
        base_order_manager._gw.client.get_open_position.return_value = nan_pos

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

        n = await sm.drain_disabled_sleeves()
        assert n == 0
        base_order_manager.place_exit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_drain_dry_run_logs_bounded_qty_on_partial(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
        caplog,
    ):
        """Dry-run path must emit the bounded qty in its log line, not
        the raw DB qty. A regression where the dry-run preview shows
        ``qty=10`` while the live path SELLs 5 would mask the partial
        bound during operator review.
        """
        import logging
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES ('SPY', 'US', 'USD', 10.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        partial = MagicMock()
        partial.qty = "5.0"
        base_order_manager._gw.client.get_open_position.return_value = partial

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
            dry_run=True,
        )

        with caplog.at_level(logging.WARNING, logger="trading_bot.strategy.strategy_manager"):
            n = await sm.drain_disabled_sleeves()

        # Dry-run skips order submission entirely; no row is closed.
        assert n == 0
        base_order_manager.place_exit.assert_not_awaited()
        # The "Draining orphan position" warning emits the bounded qty.
        drain_lines = [
            r.getMessage() for r in caplog.records
            if "Draining orphan position" in r.getMessage()
        ]
        assert drain_lines, "expected a 'Draining orphan position' log line"
        assert "qty=5.0000" in drain_lines[0], (
            f"dry-run log must show bounded qty (5), got: {drain_lines[0]}"
        )

    @pytest.mark.asyncio
    async def test_drain_does_not_close_db_on_transient_alpaca_error(
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
        """Regression for review CRITICAL-2: a transient Alpaca lookup
        failure (5xx, network, rate-limit) must NOT mark the DB row
        CLOSED. Pre-fix the broad except matched any exception and
        permanently dropped a possibly-real position from the monitoring
        loop. Now we distinguish 'position does not exist' (HTTP 404 /
        code 40410000) from any other error and treat the latter as
        UNKNOWN — skip + retry next tick.
        """
        import sqlite3
        from alpaca.common.exceptions import APIError

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                id, ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (55, 'SPY', 'US', 'USD', 1.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        # Simulate a 500 from Alpaca by raising an APIError that does
        # NOT match the position-not-found markers (no 404, no
        # 40410000, no "position does not exist" substring).
        broker_500 = APIError(
            '{"code": 50010000, "message": "internal server error"}'
        )
        base_order_manager._gw.client.get_open_position.side_effect = broker_500

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

        n = await sm.drain_disabled_sleeves()
        assert n == 0
        # No drain SELL submitted (transient error, can't confirm side).
        base_order_manager.place_exit.assert_not_awaited()

        # CRITICAL: DB row must remain OPEN — pre-fix bug closed it.
        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = 55"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "STOP_ACTIVE", (
            "transient Alpaca error must NOT mark DB CLOSED — pre-fix "
            "regression"
        )

    @pytest.mark.asyncio
    async def test_drain_does_not_close_db_on_generic_exception(
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
        """Even non-APIError exceptions (network timeout, parse error,
        attribute error from a stubbed client) must not close DB rows.
        """
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                id, ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (66, 'SPY', 'US', 'USD', 1.0, 100.0,
                      '2026-04-27T15:40:00-04:00',
                      'STOP_ACTIVE', 'swing', 1, 'breakout')
            """
        )
        conn.commit()
        conn.close()

        base_order_manager._gw.client.get_open_position.side_effect = (
            ConnectionError("network timeout")
        )

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

        n = await sm.drain_disabled_sleeves()
        assert n == 0
        base_order_manager.place_exit.assert_not_awaited()

        conn = sqlite3.connect(tmp_db_path)
        try:
            row = conn.execute(
                "SELECT status FROM positions WHERE id = 66"
            ).fetchone()
        finally:
            conn.close()
        assert row[0] == "STOP_ACTIVE"


# ---------------------------------------------------------------------------
# Within-tick over-firing (2026-04-29 incident)
# ---------------------------------------------------------------------------


class TestWithinTickMaxPositions:
    """A single scan must not exceed ``max_positions`` even when the
    in-memory portfolio doesn't see attempts (rejections, async delays).

    Reproduces the path where 12 ETFs were stamped CLOSED in one tick
    despite ``max_positions`` being far smaller.
    """

    @pytest.mark.asyncio
    async def test_attempts_count_against_max_positions(
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
        # Strategy allows 2 positions. portfolio.get_open_positions() always
        # returns [] (simulating rejected entries that go straight to CLOSED
        # and never appear as open). Manager must still cap at 2 attempts.
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
            watchlist=["SPY", "QQQ", "XLK", "XLF", "XLY"],
            get_5min_bars=fake_5min,
            get_daily_bars=fake_daily,
            account_equity_usd=1000.0,
        )
        # Hard cap at max_positions even though 5 tickers were eligible.
        assert n == 2
        assert base_order_manager.place_entry.call_count == 2


# ---------------------------------------------------------------------------
# Same-day re-entry dedup (2026-04-29 incident)
# ---------------------------------------------------------------------------


class TestSameDayReentryDedup:
    """Reject duplicate entries when a row exists for today, even CLOSED ones."""

    @pytest.mark.asyncio
    async def test_skips_when_today_row_exists_with_closed_status(
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
        # Seed a row that mirrors the 2026-04-29 incident: a CLOSED row
        # with no alpaca_order_id (rejected), entry_time = today (ET).
        import sqlite3
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et_today = datetime.now(tz=ZoneInfo("US/Eastern")).date().isoformat()
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (?, 'US', 'USD', 1.0, 100.0, ?, 'CLOSED', 'swing', 1, 'stub')
            """,
            ("SPY", f"{et_today}T11:45:41-04:00"),
        )
        conn.commit()
        conn.close()

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
    async def test_allows_when_no_today_row(
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
        # Seed a row from YESTERDAY — must NOT block today's entry.
        import sqlite3
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        et_yesterday = (
            datetime.now(tz=ZoneInfo("US/Eastern")).date() - timedelta(days=1)
        ).isoformat()
        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            """
            INSERT INTO positions (
                ticker, exchange, currency, quantity, entry_price,
                entry_time, status, hold_type, phase, strategy_id
            ) VALUES (?, 'US', 'USD', 1.0, 100.0, ?, 'CLOSED', 'swing', 1, 'stub')
            """,
            ("SPY", f"{et_yesterday}T11:45:41-04:00"),
        )
        conn.commit()
        conn.close()

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
        assert n == 1


# ---------------------------------------------------------------------------
# Cross-strategy Alpaca collision guard
# ---------------------------------------------------------------------------


class TestCrossStrategyAlpacaCollision:
    """Block BUYs that would wash-trade against another sleeve's stop.

    Alpaca rejects a BUY with code 40310000 ("potential wash trade
    detected. use complex orders") when an opposite-side stop already
    rests on the same ticker. The 2026-05-07 19:45 UTC overnight_drift
    tick hit this twice (SPY + QQQ already held by mean_reversion).
    The same-strategy ``get_open_positions`` guard misses the case
    because portfolios are scoped per-strategy_id; the live broker
    state is the source of truth.
    """

    def _alpaca_pos(self, symbol: str, qty: str = "1.0") -> Any:
        pos = MagicMock()
        pos.symbol = symbol
        pos.qty = qty
        return pos

    @pytest.mark.asyncio
    async def test_skips_when_alpaca_holds_ticker_under_other_strategy(
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
        # This strategy's in-memory portfolio is empty (it doesn't think
        # it holds SPY) — but Alpaca says SPY is held (parked there by
        # another sleeve's earlier entry).
        base_order_manager._gw.get_positions = AsyncMock(
            return_value=[self._alpaca_pos("SPY", "1.0")],
        )

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
    async def test_allows_when_alpaca_holds_unrelated_ticker(
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
        # Alpaca holds QQQ (unrelated to candidate); SPY scan must proceed.
        base_order_manager._gw.get_positions = AsyncMock(
            return_value=[self._alpaca_pos("QQQ", "1.0")],
        )

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
        assert n == 1
        base_order_manager.place_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_qty_position_does_not_block(
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
        # A freshly-closed position can linger in get_positions() with
        # qty="0" before the broker prunes it. Must NOT trigger a false
        # block — the wash-trade rejection only applies to non-zero
        # holdings with resting opposite-side stops.
        base_order_manager._gw.get_positions = AsyncMock(
            return_value=[self._alpaca_pos("SPY", "0")],
        )

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
        assert n == 1
        base_order_manager.place_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_short_position_blocks_entry(
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
        # Alpaca holds SPY short (qty="-1.0"). A long BUY would still
        # be rejected as a wash trade against the resting buy-to-cover
        # stop, so the guard must block here too — same severity as
        # the long-side collision.
        base_order_manager._gw.get_positions = AsyncMock(
            return_value=[self._alpaca_pos("SPY", "-1.0")],
        )

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
    async def test_alpaca_lookup_failure_fails_open(
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
        # If get_positions blows up (transient API error), we must NOT
        # block the entire scan — same fail-open posture used everywhere
        # else for advisory broker lookups (regime filter, sentiment).
        base_order_manager._gw.get_positions = AsyncMock(
            side_effect=RuntimeError("alpaca 503"),
        )

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
        assert n == 1
        base_order_manager.place_entry.assert_called_once()


# ---------------------------------------------------------------------------
# Same-day attempt DB-error escalation (item 5)
# ---------------------------------------------------------------------------


class TestSameDayAttemptDbErrorEscalation:
    """`_already_attempted_today` flips fail-open → fail-closed after a
    streak of DB errors.

    The original 2026-04-29 over-firing fix (per-tick same-day-attempt
    lookup) failed silently when the DB was unreachable: every ticker
    sailed through the guard, recreating the very pattern the guard
    was added to stop. A short streak is still tolerated (single
    SQLite lock from artifact-cache backup), but a sustained streak
    must skip the entry.
    """

    def _make_sm(
        self,
        base_market_data,
        base_risk_manager,
        base_earnings,
        base_sentiment,
        base_order_manager,
        base_portfolio_manager,
        base_config,
        tmp_db_path,
    ) -> StrategyManager:
        return StrategyManager(
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

    def test_single_error_fails_open(
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
        sm = self._make_sm(
            base_market_data, base_risk_manager, base_earnings, base_sentiment,
            base_order_manager, base_portfolio_manager, base_config, tmp_db_path,
        )
        # Use a path that does not exist as a directory so connect() fails.
        sm._db_path = "/nonexistent/dir/that/does/not/exist.db"
        # First call: fail open (return False).
        assert sm._already_attempted_today("SPY", "stub") is False
        assert sm._db_error_streak == 1

    def test_streak_below_threshold_fails_open(
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
        sm = self._make_sm(
            base_market_data, base_risk_manager, base_earnings, base_sentiment,
            base_order_manager, base_portfolio_manager, base_config, tmp_db_path,
        )
        sm._db_path = "/nonexistent/dir/that/does/not/exist.db"
        # Two failures (below the threshold of 3): still fail open.
        assert sm._already_attempted_today("SPY", "stub") is False
        assert sm._already_attempted_today("QQQ", "stub") is False
        assert sm._db_error_streak == 2

    def test_streak_at_threshold_fails_closed(
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
        sm = self._make_sm(
            base_market_data, base_risk_manager, base_earnings, base_sentiment,
            base_order_manager, base_portfolio_manager, base_config, tmp_db_path,
        )
        sm._db_path = "/nonexistent/dir/that/does/not/exist.db"
        # Third consecutive failure must flip to fail-closed.
        assert sm._already_attempted_today("SPY", "stub") is False
        assert sm._already_attempted_today("QQQ", "stub") is False
        assert sm._already_attempted_today("XLF", "stub") is True
        assert sm._db_error_streak == 3

    def test_success_resets_streak(
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
        sm = self._make_sm(
            base_market_data, base_risk_manager, base_earnings, base_sentiment,
            base_order_manager, base_portfolio_manager, base_config, tmp_db_path,
        )
        # Build up a streak against a bad path...
        sm._db_path = "/nonexistent/dir/that/does/not/exist.db"
        sm._already_attempted_today("SPY", "stub")
        sm._already_attempted_today("QQQ", "stub")
        assert sm._db_error_streak == 2
        # ...then heal the DB. A successful lookup must reset to zero
        # so subsequent transient errors don't immediately fail-closed.
        sm._db_path = tmp_db_path
        result = sm._already_attempted_today("SPY", "stub")
        assert result is False  # No row → not attempted today
        assert sm._db_error_streak == 0


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

    @pytest.mark.asyncio
    async def test_pending_exit_skips_evaluate(
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
        """V11+: a position with a persisted alpaca_exit_order_id has
        a pending exit order from an earlier tick. check_exits must
        skip it entirely — no evaluate_exit call, no place_exit. The
        OrderManager's poll handles fill detection (or rollback on
        cancel/expire/reject). Without this gate, the bot would
        re-submit and Alpaca would reject with
        ``held_for_orders=qty`` — the 2026-05-12/13 pattern that
        motivated the V11 work."""
        sm, strategy, portfolio = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{
                "ticker": "SPY", "entry_price": 100.0, "quantity": 5,
                "alpaca_exit_order_id": "alp-exit-pending",
            }],
            exit_signal=ExitSignal(should_exit=True, reason="overnight_exit"),
        )
        n = await sm.check_exits()
        assert n == 0, "must not double-submit when an exit is already pending"
        assert strategy.evaluate_exit_calls == [], (
            "evaluate_exit must be short-circuited for positions with "
            "a persisted alpaca_exit_order_id — the gate runs before "
            "the strategy is consulted"
        )
        portfolio.record_exit.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_exit_order_id_does_not_skip(
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
        """Sanity: an explicit NULL in the position dict (the common
        case — no pending exit) must NOT short-circuit. Catches a
        regression where someone accidentally writes ``if "key" in
        position`` instead of ``if position.get(...) is not None``."""
        sm, strategy, _ = self._setup(
            base_market_data, base_portfolio_manager, base_order_manager, base_config,
            base_risk_manager, base_sentiment, base_earnings, tmp_db_path,
            positions=[{
                "ticker": "SPY", "entry_price": 100.0, "quantity": 5,
                "alpaca_exit_order_id": None,
            }],
            exit_signal=ExitSignal(should_exit=True, reason="take_profit"),
        )
        n = await sm.check_exits()
        assert n == 1
        assert len(strategy.evaluate_exit_calls) == 1


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

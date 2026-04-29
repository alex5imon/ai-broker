"""Tests for the strategy-manager gates added with the risk-pickup batch.

Covers:
- per-symbol allocation cap (``watchlist_caps``)
- entry-limit slop clamping (``entry.limit_slop_pct``)
- per-strategy consecutive-loss cooldown
- macro-event (FOMC) size multiplier
"""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from trading_bot.constants import HoldType, PositionStatus
from trading_bot.execution.loss_cooldown import LossCooldownConfig, LossCooldownTracker
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.strategy_manager import StrategyManager


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubStrategy(StrategyBase):
    def __init__(
        self, strategy_id: str, decision: StrategyDecision | None = None,
    ) -> None:
        super().__init__(strategy_id=strategy_id, display_name=strategy_id, config={})
        self._decision = decision

    def evaluate_entry(
        self, ticker, exchange, df_5min, df_daily, current_price,
        available_cash, sentiment_score=None,
    ):
        if self._decision is None:
            return None
        return StrategyDecision(
            ticker=ticker,
            exchange=exchange,
            direction="long",
            shares=self._decision.shares,
            entry_price=current_price,
            stop_price=current_price * 0.98,
            target_price=current_price * 1.04,
            trail_pct=None,
            hold_type=HoldType.SWING,
            strategy_id=self.strategy_id,
            signals={},
            sentiment_score=sentiment_score,
        )

    def evaluate_exit(self, position, current_price, df_5min=None, df_daily=None):
        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return 5


def _bars(n: int = 30, start: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [start] * n, "high": [start * 1.01] * n,
            "low": [start * 0.99] * n, "close": [start] * n,
            "volume": [1_000_000] * n,
        }
    )


@pytest.fixture
def fake_bars():
    async def _f(_t, _e):
        return _bars()
    return _f


@pytest.fixture
def fake_daily_bars():
    async def _f(_t, _e):
        return _bars(60)
    return _f


@pytest.fixture
def market_data():
    md = MagicMock()
    md.trading_paused = False
    md.is_stale = MagicMock(return_value=False)
    md.get_latest_price = MagicMock(return_value=100.0)
    md.get_bid_ask = MagicMock(return_value=(99.95, 100.05))
    return md


@pytest.fixture
def risk_manager():
    rm = MagicMock()
    rm.can_trade = MagicMock(return_value=(True, "ok"))
    return rm


@pytest.fixture
def order_manager():
    om = MagicMock()
    om.place_entry = AsyncMock(return_value=42)
    om.place_exit = AsyncMock(return_value="alpaca-exit-1")
    return om


@pytest.fixture
def sentiment():
    sa = MagicMock()
    sa.get_sentiment = AsyncMock(return_value=0.0)
    return sa


@pytest.fixture
def earnings():
    ec = MagicMock()
    ec.is_in_blackout = MagicMock(return_value=False)
    return ec


@pytest.fixture
def portfolio_manager():
    pm = MagicMock()
    portfolio = MagicMock()
    portfolio.get_open_positions = MagicMock(return_value=[])
    portfolio.available_cash = 1000.0
    portfolio.current_cash = 1000.0
    portfolio.record_entry = MagicMock()
    portfolio.record_exit = MagicMock()
    pm.get_portfolio = MagicMock(return_value=portfolio)
    pm.get_all_portfolios = MagicMock(return_value={"stub": portfolio})
    pm._portfolio = portfolio
    return pm


def _config(**overrides) -> Any:
    cfg = MagicMock()
    phase = MagicMock()
    phase.value = 1
    cfg.get_phase = MagicMock(return_value=phase)
    # Default — no caps, no slop, FOMC disabled.
    cfg.get_symbol_max_allocation_pct = MagicMock(
        side_effect=lambda _t: overrides.get("cap_pct", 1.0),
    )
    cfg.entry_limit_slop_pct = overrides.get("slop_pct", 0.0)
    cfg._raw = overrides.get("raw", {})
    return cfg


def _decision(shares: float = 5, entry: float = 100.0) -> StrategyDecision:
    return StrategyDecision(
        ticker="SPY",
        exchange="US",
        direction="long",
        shares=shares,
        entry_price=entry,
        stop_price=entry * 0.98,
        target_price=entry * 1.04,
        trail_pct=None,
        hold_type=HoldType.SWING,
        strategy_id="stub",
        signals={},
        sentiment_score=0.0,
    )


# ---------------------------------------------------------------------------
# FOMC gate
# ---------------------------------------------------------------------------


class TestFomcGate:
    @pytest.mark.asyncio
    async def test_fomc_skip_blocks_all_entries(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path, monkeypatch,
    ):
        cfg = _config(raw={
            "event_gate": {
                "enabled": True,
                "fomc_action": "skip",
                "fomc_dates_2099": ["2099-01-01"],
            }
        })
        # Force "today" to match a configured FOMC date
        from trading_bot.strategy import strategy_manager as sm_mod
        monkeypatch.setattr(
            sm_mod, "fomc_size_multiplier",
            lambda _today, _raw: 0.0,
        )

        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision())],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        n = await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        assert n == 0
        order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_fomc_reduce_scales_share_count(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path, monkeypatch,
    ):
        cfg = _config()
        from trading_bot.strategy import strategy_manager as sm_mod
        monkeypatch.setattr(
            sm_mod, "fomc_size_multiplier", lambda _t, _r: 0.5,
        )

        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=10))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        order_manager.place_entry.assert_called_once()
        om_decision = order_manager.place_entry.call_args[0][0]
        assert om_decision.shares == 5  # 10 × 0.5


# ---------------------------------------------------------------------------
# Limit-slop clamp
# ---------------------------------------------------------------------------


class TestLimitSlopClamp:
    @pytest.mark.asyncio
    async def test_limit_clamped_above_ask_for_buy(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        # Strategy wants to buy at 105.0; ask=100.05, slop=0.2% → cap = 100.25
        market_data.get_bid_ask = MagicMock(return_value=(99.95, 100.05))
        market_data.get_latest_price = MagicMock(return_value=105.0)
        cfg = _config(slop_pct=0.002)

        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=5, entry=105.0))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        om_decision = order_manager.place_entry.call_args[0][0]
        assert om_decision.limit_price == round(100.05 * 1.002, 2)

    @pytest.mark.asyncio
    async def test_limit_inside_slop_window_unchanged(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        market_data.get_bid_ask = MagicMock(return_value=(99.95, 100.05))
        market_data.get_latest_price = MagicMock(return_value=100.10)
        cfg = _config(slop_pct=0.002)

        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=5, entry=100.10))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        om_decision = order_manager.place_entry.call_args[0][0]
        assert om_decision.limit_price == 100.10


# ---------------------------------------------------------------------------
# Per-symbol allocation cap
# ---------------------------------------------------------------------------


class TestSymbolAllocationCap:
    @pytest.mark.asyncio
    async def test_cap_shrinks_oversized_entry(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        # Total book = $1000. Cap SPY at 30% → $300 max. 5 × $100 = $500 → cap.
        cfg = _config(cap_pct=0.30)
        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=5, entry=100.0))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        order_manager.place_entry.assert_called_once()
        om_decision = order_manager.place_entry.call_args[0][0]
        assert om_decision.shares == 3  # int($300 / $100) = 3

    @pytest.mark.asyncio
    async def test_cap_rejects_when_existing_exposure_full(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        # Pre-populate an existing SPY position consuming 30% of book.
        conn: sqlite3.Connection = sqlite3.connect(tmp_db_path)
        try:
            conn.execute(
                "INSERT INTO positions "
                "(ticker, exchange, currency, quantity, entry_price, "
                " entry_time, status, hold_type, phase, strategy_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("SPY", "US", "USD", 3, 100.0, "2026-01-01T00:00:00",
                 PositionStatus.POSITION_OPEN.value, "swing", 1, "stub"),
            )
            conn.commit()
        finally:
            conn.close()

        cfg = _config(cap_pct=0.30)
        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=5, entry=100.0))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_cap_disabled_at_unity_lets_decision_through(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        cfg = _config(cap_pct=1.0)
        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision(shares=5, entry=100.0))],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        om_decision = order_manager.place_entry.call_args[0][0]
        assert om_decision.shares == 5


# ---------------------------------------------------------------------------
# Loss-cooldown gate
# ---------------------------------------------------------------------------


class TestLossCooldownGate:
    @pytest.mark.asyncio
    async def test_strategy_on_cooldown_skipped(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path,
    ):
        cfg = _config()
        tracker = LossCooldownTracker(
            db_path=tmp_db_path,
            config=LossCooldownConfig(enabled=True, threshold_losses=2),
        )
        # Engage cooldown
        tracker.record_outcome("stub", -10.0)
        tracker.record_outcome("stub", -10.0)

        sm = StrategyManager(
            strategies=[_StubStrategy("stub", _decision())],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
            loss_cooldown=tracker,
        )
        n = await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )
        assert n == 0
        order_manager.place_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_check_exits_records_loss_outcome(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, tmp_db_path,
    ):
        cfg = _config()
        tracker = LossCooldownTracker(
            db_path=tmp_db_path,
            config=LossCooldownConfig(enabled=True, threshold_losses=2),
        )
        portfolio = portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=[
            {"ticker": "SPY", "entry_price": 100.0, "quantity": 3},
        ])
        market_data.get_latest_price = MagicMock(return_value=95.0)

        strategy = _StubStrategy("stub", None)
        # Force exit
        strategy._exit_signal = ExitSignal(should_exit=True, reason="stop_loss")
        strategy.evaluate_exit = lambda *a, **kw: ExitSignal(
            should_exit=True, reason="stop_loss",
        )

        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
            loss_cooldown=tracker,
        )
        await sm.check_exits()
        # First loss recorded; not yet on cooldown (threshold=2)
        assert tracker.is_on_cooldown("stub")[0] is False
        await sm.check_exits()
        # Second consecutive loss → cooldown engaged
        assert tracker.is_on_cooldown("stub")[0] is True


# ---------------------------------------------------------------------------
# check_exits broker-order safety
# ---------------------------------------------------------------------------


class TestCheckExitsBrokerSafety:
    """Regressions for the bug where check_exits recorded virtual exits
    without ever sending a broker order — silently diverging the virtual
    portfolio from real Alpaca state on every exit signal."""

    @pytest.mark.asyncio
    async def test_check_exits_calls_broker_before_recording_virtual_exit(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, tmp_db_path,
    ):
        cfg = _config()
        portfolio = portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=[
            {"ticker": "SPY", "entry_price": 100.0, "quantity": 3},
        ])
        market_data.get_latest_price = MagicMock(return_value=95.0)

        strategy = _StubStrategy("stub", None)
        strategy.evaluate_exit = lambda *a, **kw: ExitSignal(
            should_exit=True, reason="stop_loss", is_emergency=True,
        )

        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        n = await sm.check_exits()

        assert n == 1
        # Broker order MUST have been submitted
        order_manager.place_exit.assert_called_once()
        kwargs = order_manager.place_exit.call_args.kwargs
        assert kwargs["ticker"] == "SPY"
        assert kwargs["qty"] == 3
        assert kwargs["reason"] == "stop_loss"
        assert kwargs["is_emergency"] is True
        # Virtual exit only after broker confirmation
        portfolio.record_exit.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_exits_skips_virtual_exit_when_broker_rejects(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, tmp_db_path,
    ):
        # Broker rejection → leave virtual portfolio untouched so we
        # re-attempt next tick (rather than diverging from real state).
        order_manager.place_exit = AsyncMock(return_value=None)

        cfg = _config()
        portfolio = portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=[
            {"ticker": "SPY", "entry_price": 100.0, "quantity": 3},
        ])
        market_data.get_latest_price = MagicMock(return_value=95.0)

        strategy = _StubStrategy("stub", None)
        strategy.evaluate_exit = lambda *a, **kw: ExitSignal(
            should_exit=True, reason="stop_loss",
        )

        tracker = LossCooldownTracker(
            db_path=tmp_db_path,
            config=LossCooldownConfig(enabled=True, threshold_losses=2),
        )

        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
            loss_cooldown=tracker,
        )
        n = await sm.check_exits()

        assert n == 0
        portfolio.record_exit.assert_not_called()
        # No fake loss recorded against the cooldown tracker either
        assert tracker.is_on_cooldown("stub")[0] is False

    @pytest.mark.asyncio
    async def test_check_exits_skips_stale_ticker(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, tmp_db_path,
    ):
        cfg = _config()
        # Stale-data gate should preempt the exit signal entirely so we
        # never send a phantom market sell driven by a stale price.
        market_data.is_stale = MagicMock(return_value=True)
        portfolio = portfolio_manager._portfolio
        portfolio.get_open_positions = MagicMock(return_value=[
            {"ticker": "SPY", "entry_price": 100.0, "quantity": 3},
        ])

        strategy = _StubStrategy("stub", None)
        strategy.evaluate_exit = lambda *a, **kw: ExitSignal(
            should_exit=True, reason="stop_loss",
        )

        sm = StrategyManager(
            strategies=[strategy],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        n = await sm.check_exits()
        assert n == 0
        order_manager.place_exit.assert_not_called()
        portfolio.record_exit.assert_not_called()


# ---------------------------------------------------------------------------
# Decision immutability
# ---------------------------------------------------------------------------


class TestDecisionImmutability:
    """The FOMC scaler and symbol-cap helpers must return new
    StrategyDecision objects rather than mutating the caller's."""

    def test_scale_decision_shares_does_not_mutate_input(self):
        original = _decision(shares=10, entry=100.0)
        scaled = StrategyManager._scale_decision_shares(original, 0.5)
        assert scaled is not None
        assert scaled.shares == 5
        # Original must remain untouched
        assert original.shares == 10

    def test_scale_decision_preserves_int_intent(self):
        original = _decision(shares=10, entry=100.0)
        scaled = StrategyManager._scale_decision_shares(original, 0.33)
        assert scaled is not None
        assert isinstance(scaled.shares, int)
        assert scaled.shares == 3  # int(10 * 0.33)

    def test_scale_decision_preserves_fractional_intent(self):
        original = _decision(shares=10.0, entry=100.0)
        scaled = StrategyManager._scale_decision_shares(original, 0.5)
        assert scaled is not None
        assert isinstance(scaled.shares, float)
        assert scaled.shares == 5.0

    def test_scale_decision_below_min_returns_none(self):
        original = _decision(shares=1, entry=100.0)
        scaled = StrategyManager._scale_decision_shares(original, 0.5)
        # 1 * 0.5 = 0.5 → int(0.5) = 0 → below int min of 1
        assert scaled is None
        # Original still untouched
        assert original.shares == 1

    @pytest.mark.asyncio
    async def test_pipeline_does_not_mutate_strategy_decision(
        self, market_data, risk_manager, order_manager, sentiment, earnings,
        portfolio_manager, fake_bars, fake_daily_bars, tmp_db_path, monkeypatch,
    ):
        """End-to-end: FOMC scale × symbol cap composition leaves the
        original decision the strategy returned untouched."""
        # FOMC half-size, symbol cap 30% of $1000 = $300 → max 3 shares.
        from trading_bot.strategy import strategy_manager as sm_mod
        monkeypatch.setattr(
            sm_mod, "fomc_size_multiplier", lambda _t, _r: 0.5,
        )
        cfg = _config(cap_pct=0.30)

        held: list[StrategyDecision] = []

        class _CapturingStrategy(_StubStrategy):
            def evaluate_entry(self, *a, **kw):
                d = super().evaluate_entry(*a, **kw)
                if d is not None:
                    held.append(d)
                return d

        strat = _CapturingStrategy("stub", _decision(shares=10, entry=100.0))
        sm = StrategyManager(
            strategies=[strat],
            portfolio_manager=portfolio_manager,
            market_data=market_data, order_manager=order_manager,
            risk_manager=risk_manager, sentiment=sentiment,
            earnings=earnings, config=cfg, db_path=tmp_db_path,
        )
        await sm.scan_for_entries(
            watchlist=["SPY"], get_5min_bars=fake_bars,
            get_daily_bars=fake_daily_bars, account_equity_usd=1000.0,
        )

        assert len(held) == 1
        # Strategy's original decision is preserved.
        assert held[0].shares == 10

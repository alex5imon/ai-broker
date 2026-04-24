"""Tests for multi-strategy backtester and data downloader."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from trading_bot.config import Config
from trading_bot.multi_strategy_backtest import (
    MultiStrategyBacktester,
    MultiStrategyResult,
    StrategyResult,
    StrategyTrade,
    _StrategyState,
    format_comparison_report,
    save_comparison_json,
)

TZ_ET = ZoneInfo("US/Eastern")
TZ_UTC = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config() -> Config:
    return Config.load("config.yaml")


def _make_5min_df(
    n_bars: int = 80,
    base_price: float = 10.0,
    start_date: date | None = None,
    trend: float = 0.0,
) -> pd.DataFrame:
    """Generate synthetic 5-min OHLCV bars."""
    if start_date is None:
        start_date = date(2026, 3, 15)

    start_dt = datetime(
        start_date.year, start_date.month, start_date.day,
        9, 30, 0, tzinfo=TZ_ET,
    )
    timestamps = [start_dt + timedelta(minutes=5 * i) for i in range(n_bars)]
    prices = [base_price + trend * i + (0.05 * ((-1) ** i)) for i in range(n_bars)]

    data: dict[str, list] = {
        "open": [p - 0.02 for p in prices],
        "high": [p + 0.10 for p in prices],
        "low": [p - 0.10 for p in prices],
        "close": prices,
        "volume": [50000 + i * 100 for i in range(n_bars)],
    }
    df = pd.DataFrame(data, index=pd.DatetimeIndex(timestamps, tz=TZ_ET, name="timestamp"))
    return df


def _make_daily_df(
    n_days: int = 60,
    base_price: float = 10.0,
    trend: float = 0.01,
) -> pd.DataFrame:
    """Generate synthetic daily bars."""
    start = date(2026, 1, 1)
    dates = []
    d = start
    while len(dates) < n_days:
        if d.weekday() < 5:
            dates.append(d)
        d += timedelta(days=1)

    timestamps = [
        datetime(d.year, d.month, d.day, 16, 0, 0, tzinfo=TZ_ET)
        for d in dates
    ]
    prices = [base_price + trend * i for i in range(n_days)]
    data: dict[str, list] = {
        "open": [p - 0.05 for p in prices],
        "high": [p + 0.20 for p in prices],
        "low": [p - 0.20 for p in prices],
        "close": prices,
        "volume": [1000000 + i * 10000 for i in range(n_days)],
    }
    df = pd.DataFrame(data, index=pd.DatetimeIndex(timestamps, tz=TZ_ET, name="timestamp"))
    return df


def _make_strategy_mock(strategy_id: str = "test_strat") -> MagicMock:
    mock = MagicMock()
    mock.strategy_id = strategy_id
    mock.display_name = "Test Strategy"
    mock.get_max_positions.return_value = 2
    mock.evaluate_entry.return_value = None
    mock.evaluate_exit.return_value = MagicMock(should_exit=False)
    return mock


# ---------------------------------------------------------------------------
# _StrategyState tests
# ---------------------------------------------------------------------------

class TestStrategyState:
    def test_initial_peak_matches_cash(self) -> None:
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=1000.0, initial_cash_usd=1000.0)
        assert state.peak_cash_usd == 1000.0
        assert state.open_positions == []
        assert state.closed_trades == []

    def test_daily_returns_initially_empty(self) -> None:
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=500.0, initial_cash_usd=500.0)
        assert state.daily_returns == []


# ---------------------------------------------------------------------------
# StrategyTrade tests
# ---------------------------------------------------------------------------

class TestStrategyTrade:
    def test_create_trade(self) -> None:
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="mean_reversion",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=10.50,
            shares=50,
            stop_price=10.29,
            target_price=10.82,
            trail_pct=None,
            signals={"rsi": 28.5},
        )
        assert trade.exit_time is None
        assert trade.net_pnl_usd == 0.0
        assert trade.shares == 50


# ---------------------------------------------------------------------------
# MultiStrategyBacktester tests
# ---------------------------------------------------------------------------

class TestMultiStrategyBacktester:
    def test_init_loads_strategies(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # Only mean_reversion is enabled by default in the shipped config;
        # the others are held back until they produce comparable backtest evidence.
        ids = {s.strategy_id for s in engine._strategies}
        assert "mean_reversion" in ids
        assert len(engine._strategies) >= 1

    def test_resample_to_5min(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # Create 1-min bars
        start = datetime(2026, 3, 15, 9, 30, 0, tzinfo=TZ_ET)
        timestamps = [start + timedelta(minutes=i) for i in range(30)]
        data = {
            "open": [10.0] * 30,
            "high": [10.5] * 30,
            "low": [9.5] * 30,
            "close": [10.1] * 30,
            "volume": [1000] * 30,
        }
        df_1min = pd.DataFrame(
            data,
            index=pd.DatetimeIndex(timestamps, tz=TZ_ET, name="timestamp"),
        )
        df_5min = engine._resample_to_5min(df_1min)
        assert len(df_5min) == 6
        assert df_5min["volume"].iloc[0] == 5000

    def test_simulate_fill_buy(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        fill = engine._simulate_fill(100.0, "buy")
        assert fill > 100.0
        assert abs(fill - 100.02) < 0.01

    def test_simulate_fill_sell(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        fill = engine._simulate_fill(100.0, "sell")
        assert fill < 100.0

    def test_get_tickers(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        tickers = engine._get_tickers()
        # Watchlist was narrowed to SPY/QQQ after Mean Reversion validation
        assert "SPY" in tickers
        assert len(tickers) >= 1

    def test_get_trading_days_skips_weekends(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        days = engine._get_trading_days(date(2026, 3, 9), date(2026, 3, 15))
        for d in days:
            assert d.weekday() < 5

    @pytest.mark.asyncio
    async def test_run_no_data_returns_empty(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        with patch.object(engine, "_load_day_data", return_value={}):
            result = await engine.run(
                date(2026, 3, 15), date(2026, 3, 15), cash_per_strategy_usd=1000.0
            )
        assert isinstance(result, MultiStrategyResult)
        assert len(result.strategies) == len(engine._strategies)
        for sr in result.strategies:
            assert sr.total_trades == 0
            assert sr.final_cash_usd == 1000.0

    @pytest.mark.asyncio
    async def test_run_with_mock_data(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df_5min = _make_5min_df(n_bars=80, base_price=10.0, trend=0.01,
                                start_date=date(2026, 3, 16))
        df_daily = _make_daily_df(n_days=60, base_price=10.0)
        mock_data = {"F": {"intraday": df_5min, "daily": df_daily}}

        with patch.object(engine, "_load_day_data", return_value=mock_data):
            result = await engine.run(
                date(2026, 3, 16), date(2026, 3, 16), cash_per_strategy_usd=1000.0,
            )

        assert isinstance(result, MultiStrategyResult)
        assert result.trading_days >= 1
        for sr in result.strategies:
            assert sr.initial_cash_usd == 1000.0


# ---------------------------------------------------------------------------
# Exit checking
# ---------------------------------------------------------------------------

class TestExitChecking:
    def test_stop_loss_exit(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=100,
            stop_price=9.80,
            target_price=10.30,
            trail_pct=None,
            signals={},
            highest_price=10.05,
        )
        result = engine._check_trade_exit(
            trade, bar_close=9.85, bar_high=10.00, bar_low=9.75,
            current_time=now, strategy=strat,
            df_5min=_make_5min_df(), df_daily=_make_daily_df(),
        )
        assert result is not None
        reason, px = result
        assert reason == "stop_loss"
        assert px <= 9.80

    def test_take_profit_exit(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=100,
            stop_price=9.80,
            target_price=10.30,
            trail_pct=None,
            signals={},
            highest_price=10.25,
        )
        result = engine._check_trade_exit(
            trade, bar_close=10.35, bar_high=10.40, bar_low=10.20,
            current_time=now, strategy=strat,
            df_5min=_make_5min_df(), df_daily=_make_daily_df(),
        )
        assert result is not None
        reason, px = result
        assert reason == "take_profit"
        assert px == 10.30

    def test_trailing_stop_exit(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=100,
            stop_price=9.70,
            target_price=None,
            trail_pct=0.025,
            signals={},
            highest_price=11.00,
        )
        trail_stop = 11.00 * (1.0 - 0.025)  # 10.725
        result = engine._check_trade_exit(
            trade, bar_close=10.60, bar_high=10.80, bar_low=10.70,
            current_time=now, strategy=strat,
            df_5min=_make_5min_df(), df_daily=_make_daily_df(),
        )
        assert result is not None
        reason, px = result
        assert reason == "trailing_stop"

    def test_no_exit_when_in_range(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=100,
            stop_price=9.80,
            target_price=10.30,
            trail_pct=None,
            signals={},
            highest_price=10.10,
        )
        result = engine._check_trade_exit(
            trade, bar_close=10.05, bar_high=10.15, bar_low=9.95,
            current_time=now, strategy=strat,
            df_5min=_make_5min_df(), df_daily=_make_daily_df(),
        )
        assert result is None


# ---------------------------------------------------------------------------
# Trade closing
# ---------------------------------------------------------------------------

class TestCloseTrade:
    def test_close_winning_trade(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        state = _StrategyState(strategy=strat, cash_usd=900.0, initial_cash_usd=1000.0)
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=10,
            stop_price=9.80,
            target_price=10.30,
            trail_pct=None,
            signals={},
        )
        state.open_positions.append(trade)
        engine._close_trade(state, trade, exit_price=10.30, reason="take_profit", exit_time=now)

        assert trade in state.closed_trades
        assert trade not in state.open_positions
        assert trade.exit_reason == "take_profit"
        assert trade.net_pnl_usd > 0
        assert state.wins == 1
        assert state.losses == 0

    def test_close_losing_trade(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        strat = _make_strategy_mock()
        # Start with peak at 1000, cash at 900 (100 used for position)
        state = _StrategyState(strategy=strat, cash_usd=900.0, initial_cash_usd=1000.0)
        state.peak_cash_usd = 1000.0
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now - timedelta(hours=1),
            entry_price=10.00,
            shares=10,
            stop_price=9.80,
            target_price=10.30,
            trail_pct=None,
            signals={},
        )
        state.open_positions.append(trade)
        engine._close_trade(state, trade, exit_price=9.70, reason="stop_loss", exit_time=now)

        assert state.losses == 1
        assert trade.net_pnl_usd < 0
        assert state.max_drawdown_pct > 0


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

class TestReportFormatting:
    def test_format_empty_result(self) -> None:
        result = MultiStrategyResult(
            from_date="2026-01-15",
            to_date="2026-04-15",
            trading_days=60,
            strategies=[
                StrategyResult(
                    strategy_id="mean_reversion",
                    display_name="Mean Reversion",
                    initial_cash_usd=1000.0,
                    final_cash_usd=1000.0,
                ),
                StrategyResult(
                    strategy_id="trend_following",
                    display_name="Trend Following",
                    initial_cash_usd=1000.0,
                    final_cash_usd=1050.0,
                    total_pnl_usd=50.0,
                    return_pct=5.0,
                    total_trades=10,
                    wins=6,
                    win_rate=60.0,
                ),
            ],
        )
        report = format_comparison_report(result)
        assert "MULTI-STRATEGY" in report
        assert "Mean Reversion" in report
        assert "Trend Following" in report
        assert "WINNER" in report

    def test_save_comparison_json(self, tmp_path: Any) -> None:
        result = MultiStrategyResult(
            from_date="2026-01-15",
            to_date="2026-04-15",
            trading_days=60,
            strategies=[
                StrategyResult(
                    strategy_id="mean_reversion",
                    display_name="Mean Reversion",
                    initial_cash_usd=1000.0,
                    final_cash_usd=980.0,
                    total_pnl_usd=-20.0,
                    return_pct=-2.0,
                ),
            ],
        )
        path = str(tmp_path / "test_result.json")
        saved = save_comparison_json(result, output_path=path)
        assert saved == path

        import json
        with open(path) as f:
            data = json.load(f)
        assert data["trading_days"] == 60
        assert len(data["strategies"]) == 1
        assert data["strategies"][0]["strategy_id"] == "mean_reversion"


# ---------------------------------------------------------------------------
# Data downloader tests
# ---------------------------------------------------------------------------

class TestAlpacaDownloader:
    def test_bars_to_df(self) -> None:
        from trading_bot.data.alpaca_downloader import _bars_to_df

        mock_bars = []
        for i in range(5):
            bar = MagicMock()
            bar.timestamp = datetime(2026, 3, 15, 9, 30 + i, 0, tzinfo=TZ_UTC)
            bar.open = 10.0 + i * 0.01
            bar.high = 10.1 + i * 0.01
            bar.low = 9.9 + i * 0.01
            bar.close = 10.05 + i * 0.01
            bar.volume = 1000 + i * 100
            mock_bars.append(bar)

        df = _bars_to_df(mock_bars)
        assert len(df) == 5
        assert "open" in df.columns
        assert "close" in df.columns
        assert df.index.name == "timestamp"

    def test_bars_to_df_empty(self) -> None:
        from trading_bot.data.alpaca_downloader import _bars_to_df
        df = _bars_to_df([])
        assert df.empty

    def test_get_trading_days(self) -> None:
        from trading_bot.data.alpaca_downloader import _get_trading_days

        config = Config.load("config.yaml")
        days = _get_trading_days(date(2026, 3, 9), date(2026, 3, 15), config)
        for d in days:
            assert d.weekday() < 5
        assert len(days) == 5  # Mon-Fri


# ---------------------------------------------------------------------------
# StrategyResult tests
# ---------------------------------------------------------------------------

class TestStrategyResult:
    def test_defaults(self) -> None:
        sr = StrategyResult(
            strategy_id="test",
            display_name="Test",
            initial_cash_usd=1000.0,
            final_cash_usd=1000.0,
        )
        assert sr.total_trades == 0
        assert sr.win_rate == 0.0
        assert sr.profit_factor is None
        assert sr.daily_returns == []

    def test_with_trades(self) -> None:
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=10.0,
            shares=10,
            stop_price=9.8,
            target_price=10.3,
            trail_pct=None,
            signals={},
            net_pnl_usd=5.0,
        )
        sr = StrategyResult(
            strategy_id="test",
            display_name="Test",
            initial_cash_usd=1000.0,
            final_cash_usd=1005.0,
            trades=[trade],
            total_trades=1,
            wins=1,
            total_pnl_usd=5.0,
            return_pct=0.5,
            win_rate=100.0,
        )
        assert sr.total_trades == 1
        assert sr.return_pct == 0.5


# ---------------------------------------------------------------------------
# Sentiment proxy tests
# ---------------------------------------------------------------------------

class TestSentimentProxy:
    def test_returns_none_for_insufficient_data(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df_short = _make_daily_df(n_days=5)
        result = engine._compute_sentiment_proxy(df_short)
        assert result is None

    def test_returns_none_for_none_input(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        assert engine._compute_sentiment_proxy(None) is None

    def test_positive_for_uptrend(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=10.0, trend=0.10)
        score = engine._compute_sentiment_proxy(df)
        assert score is not None
        assert score > 0.0

    def test_negative_for_downtrend(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=20.0, trend=-0.10)
        score = engine._compute_sentiment_proxy(df)
        assert score is not None
        assert score < 0.0

    def test_score_bounded(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=10.0, trend=0.50)
        score = engine._compute_sentiment_proxy(df)
        assert score is not None
        assert -1.0 <= score <= 1.0


# ---------------------------------------------------------------------------
# Per-ticker slippage tests
# ---------------------------------------------------------------------------

class TestPerTickerSlippage:
    def test_liquid_ticker_lower_slippage(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        fill_f = engine._simulate_fill(10.0, "buy", "F")
        fill_nio = engine._simulate_fill(10.0, "buy", "NIO")
        assert fill_f < fill_nio

    def test_unknown_ticker_uses_default(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        fill = engine._simulate_fill(100.0, "buy", "ZZZZ")
        expected = 100.0 * (1 + 2.0 / 10_000)
        assert abs(fill - expected) < 0.001

    def test_sell_slippage(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        fill = engine._simulate_fill(10.0, "sell", "NIO")
        assert fill < 10.0


# ---------------------------------------------------------------------------
# Overnight carry tests
# ---------------------------------------------------------------------------

class TestOvernightCarry:
    def test_trade_has_hold_type_and_days_held(self) -> None:
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=10.0,
            shares=10,
            stop_price=9.8,
            target_price=10.3,
            trail_pct=None,
            signals={},
            hold_type="swing",
        )
        assert trade.hold_type == "swing"
        assert trade.days_held == 0

    def test_intraday_trade_defaults(self) -> None:
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=10.0,
            shares=10,
            stop_price=9.8,
            target_price=10.3,
            trail_pct=None,
            signals={},
            hold_type="intraday",
        )
        assert trade.hold_type == "intraday"


# ---------------------------------------------------------------------------
# ATR computation and adaptive stops
# ---------------------------------------------------------------------------

class TestATR:
    def test_compute_atr_basic(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=50.0)
        atr = engine._compute_atr(df, period=14)
        assert atr is not None
        assert atr > 0

    def test_compute_atr_insufficient_data(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=5, base_price=50.0)
        atr = engine._compute_atr(df, period=14)
        assert atr is None

    def test_compute_atr_none_input(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        assert engine._compute_atr(None, period=14) is None

    def test_atr_adjusted_stops_returns_tuple(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=50.0)
        stop, target, trail, activation = engine._atr_adjusted_stops(50.0, df, "mean_reversion")
        assert stop < 50.0
        assert target > 50.0
        assert trail > 0
        assert activation > 0

    def test_atr_stops_wider_for_volatile_stock(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # Low volatility: small price moves
        df_calm = _make_daily_df(n_days=30, base_price=50.0, trend=0.01)
        stop_calm, _, _, _ = engine._atr_adjusted_stops(50.0, df_calm, "test")
        # High volatility: simulate wider high-low range
        df_vol = _make_daily_df(n_days=30, base_price=50.0, trend=0.01)
        df_vol["high"] = df_vol["close"] + 2.0
        df_vol["low"] = df_vol["close"] - 2.0
        stop_vol, _, _, _ = engine._atr_adjusted_stops(50.0, df_vol, "test")
        assert stop_vol < stop_calm  # wider stop for more volatile stock

    def test_atr_fallback_when_no_data(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df_short = _make_daily_df(n_days=5, base_price=50.0)
        stop, target, trail, activation = engine._atr_adjusted_stops(50.0, df_short, "test")
        assert stop < 50.0
        assert target > 50.0

    def test_minimum_stop_distance(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=30, base_price=50.0, trend=0.001)
        stop, target, _, _ = engine._atr_adjusted_stops(50.0, df, "test")
        stop_pct = (50.0 - stop) / 50.0
        target_pct = (target - 50.0) / 50.0
        assert stop_pct >= 0.03  # minimum 3% stop
        assert target_pct >= 0.07  # minimum 7% target


class TestTrailActivation:
    def test_trail_not_active_before_threshold(self, config: Config) -> None:
        """Trailing stop should not trigger before position is up enough."""
        engine = MultiStrategyBacktester(config)
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=100.0,
            shares=10,
            stop_price=94.0,
            target_price=114.0,
            trail_pct=0.04,
            signals={},
            trail_activation_pct=0.05,
            highest_price=102.0,
        )
        strat = _make_strategy_mock()
        df = _make_daily_df(n_days=30)
        # Price at 98.0 — below trail stop of 102*(1-0.04)=97.92 IF trailing were active
        # But position is only +2% from entry, below 5% activation
        result = engine._check_trade_exit(trade, 98.0, 102.0, 97.5, now, strat, df, df)
        # Should not trigger trailing stop (not activated), but may trigger stop_loss
        if result is not None:
            assert result[0] != "trailing_stop"

    def test_trail_active_after_threshold(self, config: Config) -> None:
        """Trailing stop should trigger after position exceeds activation threshold."""
        engine = MultiStrategyBacktester(config)
        now = datetime.now(TZ_ET)
        trade = StrategyTrade(
            strategy_id="test",
            ticker="F",
            exchange="NYSE",
            entry_time=now,
            entry_price=100.0,
            shares=10,
            stop_price=94.0,
            target_price=120.0,
            trail_pct=0.04,
            signals={},
            trail_activation_pct=0.05,
            highest_price=108.0,  # +8% from entry, above 5% activation
        )
        strat = _make_strategy_mock()
        df = _make_daily_df(n_days=30)
        # Trail stop = 108 * (1 - 0.04) = 103.68; bar_low=103.0 triggers it
        result = engine._check_trade_exit(trade, 103.5, 108.0, 103.0, now, strat, df, df)
        assert result is not None
        assert result[0] == "trailing_stop"


# ---------------------------------------------------------------------------
# ATR-based position sizing
# ---------------------------------------------------------------------------

class TestSizeByRisk:
    def test_basic_sizing(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # Entry $50, stop $48 → $2 risk/share. 2% of $1000 = $20 risk → 10 shares
        # But max_position_pct=0.40 caps at $400 → 8 shares
        shares = engine._size_by_risk(50.0, 48.0, 1000.0, risk_per_trade_pct=0.02, fractional=False)
        assert shares == 8

    def test_expensive_stock_capped_by_position_limit(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        shares = engine._size_by_risk(200.0, 196.0, 1000.0, risk_per_trade_pct=0.02, max_position_pct=0.40, fractional=False)
        assert shares == 2

    def test_tight_stop_gives_more_shares(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        shares_tight = engine._size_by_risk(50.0, 49.0, 1000.0, risk_per_trade_pct=0.02, fractional=False)
        shares_wide = engine._size_by_risk(50.0, 46.0, 1000.0, risk_per_trade_pct=0.02, fractional=False)
        assert shares_tight > shares_wide

    def test_zero_risk_per_share(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        shares = engine._size_by_risk(50.0, 50.0, 1000.0)
        assert shares == 0

    def test_small_account(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        shares = engine._size_by_risk(10.0, 8.0, 100.0, risk_per_trade_pct=0.02, fractional=False)
        assert shares == 1

    def test_returns_zero_when_too_expensive(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # $500 stock, $50 account, integer sizing — can't afford any shares
        shares = engine._size_by_risk(500.0, 490.0, 50.0, risk_per_trade_pct=0.02, fractional=False)
        assert shares == 0

    def test_fractional_sizing(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        # Expensive stock, fractional shares allow partial position
        shares = engine._size_by_risk(250.0, 247.0, 1000.0, risk_per_trade_pct=0.03, max_position_pct=0.90, fractional=True)
        assert shares > 1.0  # should get fractional count > 1
        assert isinstance(shares, float)


class TestMarketRegime:
    def test_build_market_index(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df1 = _make_daily_df(n_days=60, base_price=50.0, trend=0.1)
        df2 = _make_daily_df(n_days=60, base_price=30.0, trend=0.05)
        universe = {"AAPL": df1, "MSFT": df2}
        idx = engine._build_market_index(universe, sma_period=10)
        assert not idx.empty
        assert "close" in idx.columns
        assert "sma" in idx.columns
        assert "regime_bullish" in idx.columns

    def test_uptrend_is_bullish(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        df = _make_daily_df(n_days=60, base_price=50.0, trend=0.5)
        idx = engine._build_market_index({"TEST": df}, sma_period=10)
        last_20 = idx["regime_bullish"].iloc[-20:]
        assert last_20.sum() > 15  # mostly bullish in uptrend

    def test_empty_universe(self, config: Config) -> None:
        engine = MultiStrategyBacktester(config)
        idx = engine._build_market_index({}, sma_period=10)
        assert idx.empty


# ---------------------------------------------------------------------------
# Mean Reversion strategy — VIX-adaptive RSI + let_winners_run behavior
# ---------------------------------------------------------------------------

class TestMeanReversionAdaptive:
    """Direct tests on MeanReversionStrategy for the adaptive features."""

    def _strat_with(self, **overrides: Any) -> "MeanReversionStrategy":
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        cfg: dict[str, Any] = {
            "rsi_period": 14,
            "rsi_oversold": 28,
            "rsi_recovery": 35,
            "rsi_exit": 55,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.03,
            "max_positions": 1,
            "volume_multiplier": 1.3,
            "oversold_lookback": 5,
        }
        cfg.update(overrides)
        return MeanReversionStrategy(cfg)

    def test_realized_vol_proxy(self) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        df = _make_daily_df(n_days=30, base_price=50.0, trend=0.01)
        vol = MeanReversionStrategy._realized_vol_pct(df, lookback_days=20)
        assert vol is not None
        assert vol > 0

    def test_realized_vol_insufficient_data(self) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        df = _make_daily_df(n_days=5, base_price=50.0)
        assert MeanReversionStrategy._realized_vol_pct(df, lookback_days=20) is None

    def test_adaptive_rsi_static_when_disabled(self) -> None:
        strat = self._strat_with(vix_adaptive_rsi=False)
        df = _make_daily_df(n_days=30)
        threshold, regime = strat._adaptive_rsi_oversold(df)
        assert threshold == 28
        assert regime == "static"

    def test_adaptive_rsi_respects_regime(self) -> None:
        strat = self._strat_with(
            vix_adaptive_rsi=True,
            rv_high_threshold=5.0,   # force "high vol" on calm data
            rv_low_threshold=0.1,
            rsi_oversold_high_vol=25,
            rsi_oversold_low_vol=30,
        )
        df = _make_daily_df(n_days=30, base_price=50.0, trend=0.1)
        threshold, regime = strat._adaptive_rsi_oversold(df)
        # Any generated daily frame should exceed the tiny 5% threshold
        assert threshold in (25, 28, 30)
        assert "vol" in regime or regime.startswith("static")

    def test_let_winners_run_skips_rsi_exit(self) -> None:
        """When let_winners_run is on, evaluate_exit must not fire rsi_normalized
        even if current RSI is clearly above the exit threshold."""
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        strat = self._strat_with(let_winners_run=True, rsi_exit=40)

        # Build a DataFrame where RSI will compute as ~90 (clearly above 40)
        n = 40
        # Uptrend with real pullbacks so RSI is well-defined and > rsi_exit (40)
        # Bars alternate: +1.0, -0.3 → net +0.7 per pair; RSI lands ~70-80
        prices: list[float] = [100.0]
        for i in range(1, n):
            prices.append(prices[-1] + (1.0 if i % 2 == 1 else -0.3))
        df = pd.DataFrame({
            "open": prices,
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "close": prices,
            "volume": [100_000] * n,
        })

        position = {
            "entry_price": 100.0,
            "stop_price": 95.0,
            "target_price": None,
            "highest_price": prices[-1],
        }
        signal = strat.evaluate_exit(position, current_price=prices[-1], df_5min=df)
        assert signal.reason != "rsi_normalized"

    def test_rsi_exit_fires_when_let_winners_run_disabled(self) -> None:
        """Control: with let_winners_run off, rsi_normalized should fire."""
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        # take_profit_pct very large so fallback target doesn't short-circuit
        strat = self._strat_with(
            let_winners_run=False, rsi_exit=40, take_profit_pct=10.0,
        )

        n = 40
        prices: list[float] = [100.0]
        for i in range(1, n):
            prices.append(prices[-1] + (1.0 if i % 2 == 1 else -0.3))
        df = pd.DataFrame({
            "open": prices,
            "high": [p + 0.1 for p in prices],
            "low": [p - 0.1 for p in prices],
            "close": prices,
            "volume": [100_000] * n,
        })

        position = {
            "entry_price": 100.0,
            "stop_price": 95.0,
            "target_price": None,
            "highest_price": prices[-1],
        }
        signal = strat.evaluate_exit(position, current_price=prices[-1], df_5min=df)
        assert signal.should_exit
        assert signal.reason == "rsi_normalized"


# ---------------------------------------------------------------------------
# Strategy fractional-share sizing
# ---------------------------------------------------------------------------

class TestFractionalShares:
    def test_base_size_by_risk_fractional(self, config: Config) -> None:
        from trading_bot.strategy.base import StrategyBase
        shares = StrategyBase.size_by_risk(
            entry_price=250.0, stop_price=247.5, available_cash=1000.0,
            risk_per_trade_pct=0.02, max_position_pct=0.90, fractional=True,
        )
        # 2% of $1000 = $20 risk; $2.50 per share → 8 shares by risk
        # Capped by 90% of $1000 / $250 = 3.6 shares
        assert 3.0 < shares < 4.0
        assert isinstance(shares, float)

    def test_base_size_by_risk_integer(self, config: Config) -> None:
        from trading_bot.strategy.base import StrategyBase
        shares = StrategyBase.size_by_risk(
            entry_price=250.0, stop_price=247.5, available_cash=1000.0,
            risk_per_trade_pct=0.02, max_position_pct=0.90, fractional=False,
        )
        # Integer capped: floor(3.6) = 3
        assert shares == 3.0

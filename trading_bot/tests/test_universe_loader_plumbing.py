"""Tests for the universe daily-loader pre-fetch + injection plumbing.

Covers:
- ``StrategyBase`` default hooks return empty / no-op.
- ``CrossSectionalMomentumStrategy`` overrides them correctly.
- ``StrategyManager._refresh_universe_loaders`` gathers tickers from all
  strategies, fetches in parallel via the supplied ``get_daily_bars``
  coroutine, and injects a sync closure-loader.
- The injected loader actually serves the pre-fetched bars to the
  strategy on the next ``evaluate_entry`` call.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_bot.strategy.base import StrategyBase
from trading_bot.strategy.strategies.cross_sectional_momentum import (
    CrossSectionalMomentumStrategy,
)


ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Fixtures (mirror the per-strategy test file so the plumbing test is
# self-contained and doesn't import private helpers).
# ---------------------------------------------------------------------------


def _daily_bars(
    n: int, end_date: date, total_return: float, start_price: float = 100.0,
) -> pd.DataFrame:
    end_price = start_price * (1.0 + total_return)
    prices = np.linspace(start_price, end_price, n)
    dates: list[pd.Timestamp] = []
    d = end_date
    while len(dates) < n:
        if d.weekday() < 5:
            dates.append(pd.Timestamp(d, tz=ET))
        d -= timedelta(days=1)
    dates.reverse()
    return pd.DataFrame(
        {
            "open": prices, "high": prices * 1.005, "low": prices * 0.995,
            "close": prices, "volume": np.full(n, 1_000_000),
        },
        index=pd.DatetimeIndex(dates, name="timestamp"),
    )


def _five_min_bar(ts_et: datetime, close: float = 100.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [close], "high": [close * 1.001], "low": [close * 0.999],
            "close": [close], "volume": [10_000],
        },
        index=pd.DatetimeIndex([pd.Timestamp(ts_et).tz_convert(ET)], name="timestamp"),
    )


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "universe": ["XLF", "XLK", "XLE", "XLV"],
        "lookback_days": 60, "skip_recent_days": 0, "top_n": 2,
        "max_positions": 2, "rebalance_day_of_month": 1,
        "rebalance_time_et": "09:35", "disaster_stop_pct": 0.15,
        "fractional_shares": True, "position_pct": 0.95,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# StrategyBase defaults
# ---------------------------------------------------------------------------


class _StubStrategy(StrategyBase):
    """Minimal subclass that doesn't override the universe hooks."""

    def __init__(self) -> None:
        super().__init__(
            strategy_id="stub", display_name="Stub", config={}, db_path=None,
        )

    def evaluate_entry(self, *args: Any, **kwargs: Any) -> None:
        return None

    def evaluate_exit(self, *args: Any, **kwargs: Any) -> Any:
        from trading_bot.strategy.base import ExitSignal
        return ExitSignal(should_exit=False)

    def get_max_positions(self) -> int:
        return 1


def test_base_get_universe_tickers_default_empty() -> None:
    assert _StubStrategy().get_universe_tickers() == ()


def test_base_set_universe_daily_loader_is_no_op() -> None:
    s = _StubStrategy()
    # Should not raise, no observable effect
    s.set_universe_daily_loader(lambda t, d: None)


def test_cross_sectional_get_universe_tickers_reflects_config() -> None:
    s = CrossSectionalMomentumStrategy(config=_config())
    assert s.get_universe_tickers() == ("XLF", "XLK", "XLE", "XLV")


def test_set_universe_daily_loader_invalidates_memo_and_swaps_loader() -> None:
    """First loader produces ranking A; after swap, second loader produces
    a *different* ranking — proves the memo was reset and the new loader
    is being consulted."""
    reb = date(2026, 1, 2)
    bars_a = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.30),   # winner A
        "XLE": _daily_bars(80, reb, total_return=-0.10),
        "XLV": _daily_bars(80, reb, total_return=0.18),
    }
    bars_b = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=-0.30),  # XLK now last
        "XLE": _daily_bars(80, reb, total_return=0.40),   # winner B
        "XLV": _daily_bars(80, reb, total_return=0.18),
    }

    s = CrossSectionalMomentumStrategy(
        config=_config(top_n=1),
        universe_daily_loader=lambda t, _d: bars_a.get(t),
    )
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)

    dec_a = s.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars_a["XLK"], current_price=130.0, available_cash=1500.0,
    )
    assert dec_a is not None  # XLK wins under loader A

    # Swap loader; memo must be invalidated
    s.set_universe_daily_loader(lambda t, _d: bars_b.get(t))

    dec_b_xlk = s.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars_b["XLK"], current_price=130.0, available_cash=1500.0,
    )
    dec_b_xle = s.evaluate_entry(
        ticker="XLE", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars_b["XLE"], current_price=140.0, available_cash=1500.0,
    )
    assert dec_b_xlk is None        # XLK no longer top
    assert dec_b_xle is not None    # XLE wins under loader B


# ---------------------------------------------------------------------------
# StrategyManager._refresh_universe_loaders
# ---------------------------------------------------------------------------


class _FakePortfolioManager:
    def get_portfolio(self, _sid: str) -> None:
        return None


class _FakeMarketData:
    trading_paused: bool = False

    def is_stale(self, _t: str) -> bool:
        return False

    def get_latest_price(self, _t: str) -> float:
        return 100.0


@pytest.mark.asyncio
async def test_refresh_universe_loaders_no_op_when_no_universe() -> None:
    """When no strategy declares a universe, get_daily_bars is never called."""
    from trading_bot.strategy.strategy_manager import StrategyManager

    stub = _StubStrategy()
    sm = StrategyManager(
        strategies=[stub], portfolio_manager=_FakePortfolioManager(),
        market_data=_FakeMarketData(), order_manager=None,
        risk_manager=None, sentiment=None, earnings=None,
        config=None, db_path=":memory:",
    )
    call_count = {"n": 0}

    async def _fetcher(_t: str, _country: str) -> pd.DataFrame | None:
        call_count["n"] += 1
        return None

    await sm._refresh_universe_loaders(_fetcher)
    assert call_count["n"] == 0


@pytest.mark.asyncio
async def test_refresh_universe_loaders_fetches_and_injects() -> None:
    """With a cross-sectional strategy, fetcher is called for every
    universe ticker (in parallel) and the resulting loader serves
    those bars to the strategy."""
    from trading_bot.strategy.strategy_manager import StrategyManager

    reb = date(2026, 1, 2)
    bars_by_ticker = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.30),
        "XLE": _daily_bars(80, reb, total_return=-0.10),
        "XLV": _daily_bars(80, reb, total_return=0.18),
    }
    seen: list[str] = []

    async def _fetcher(ticker: str, _country: str) -> pd.DataFrame | None:
        seen.append(ticker)
        return bars_by_ticker.get(ticker)

    csm = CrossSectionalMomentumStrategy(config=_config(top_n=2))
    sm = StrategyManager(
        strategies=[csm], portfolio_manager=_FakePortfolioManager(),
        market_data=_FakeMarketData(), order_manager=None,
        risk_manager=None, sentiment=None, earnings=None,
        config=None, db_path=":memory:",
    )
    await sm._refresh_universe_loaders(_fetcher)

    # Fetcher called exactly once per universe ticker.
    assert sorted(seen) == ["XLE", "XLF", "XLK", "XLV"]

    # Strategy now uses the freshly-injected loader.
    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    dec_xlk = csm.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=bars_by_ticker["XLK"], current_price=130.0,
        available_cash=1500.0,
    )
    assert dec_xlk is not None


@pytest.mark.asyncio
async def test_refresh_universe_loaders_tolerates_fetch_failure() -> None:
    """If get_daily_bars raises for one ticker, others still load and
    the strategy silently excludes the broken ticker from ranking."""
    from trading_bot.strategy.strategy_manager import StrategyManager

    reb = date(2026, 1, 2)
    good_bars = {
        "XLF": _daily_bars(80, reb, total_return=0.05),
        "XLK": _daily_bars(80, reb, total_return=0.30),
        "XLV": _daily_bars(80, reb, total_return=0.18),
    }

    async def _fetcher(ticker: str, _country: str) -> pd.DataFrame | None:
        if ticker == "XLE":
            raise RuntimeError("simulated alpaca outage")
        return good_bars.get(ticker)

    csm = CrossSectionalMomentumStrategy(config=_config(top_n=2))
    sm = StrategyManager(
        strategies=[csm], portfolio_manager=_FakePortfolioManager(),
        market_data=_FakeMarketData(), order_manager=None,
        risk_manager=None, sentiment=None, earnings=None,
        config=None, db_path=":memory:",
    )
    await sm._refresh_universe_loaders(_fetcher)

    ts = datetime(2026, 1, 2, 9, 35, tzinfo=ET)
    # XLK still wins out of {XLF, XLK, XLV} (XLE excluded)
    dec_xlk = csm.evaluate_entry(
        ticker="XLK", exchange="NYSE", df_5min=_five_min_bar(ts),
        df_daily=good_bars["XLK"], current_price=130.0, available_cash=1500.0,
    )
    assert dec_xlk is not None


@pytest.mark.asyncio
async def test_refresh_universe_loaders_handles_none_get_daily_bars() -> None:
    """If the caller passes None for get_daily_bars, the helper is a no-op."""
    from trading_bot.strategy.strategy_manager import StrategyManager

    csm = CrossSectionalMomentumStrategy(config=_config())
    sm = StrategyManager(
        strategies=[csm], portfolio_manager=_FakePortfolioManager(),
        market_data=_FakeMarketData(), order_manager=None,
        risk_manager=None, sentiment=None, earnings=None,
        config=None, db_path=":memory:",
    )
    # Should not raise.
    await sm._refresh_universe_loaders(None)

"""Tests for the walkforward harness and bootstrap CI utilities."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trading_bot.backtest.walkforward import (
    BootstrapCI,
    WalkforwardConfig,
    bootstrap_metric_ci,
    metric_mean_return,
    metric_profit_factor,
    metric_sharpe_approx,
    metric_win_rate,
    run_walkforward,
)
from trading_bot.multi_strategy_backtest import (
    MultiStrategyResult,
    StrategyResult,
    StrategyTrade,
)

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    pnl: float,
    entry_price: float = 100.0,
    shares: float = 1.0,
    strategy_id: str = "mr",
) -> StrategyTrade:
    return StrategyTrade(
        strategy_id=strategy_id,
        ticker="SPY",
        exchange="NASDAQ",
        entry_time=datetime(2026, 1, 1, 10, 0, tzinfo=ET),
        entry_price=entry_price,
        shares=shares,
        stop_price=entry_price * 0.98,
        target_price=None,
        trail_pct=None,
        signals={},
        exit_time=datetime(2026, 1, 1, 11, 0, tzinfo=ET),
        exit_price=entry_price + pnl / shares,
        exit_reason="target",
        gross_pnl_usd=pnl,
        net_pnl_usd=pnl,
    )


def _multi_result(
    from_date: date,
    to_date: date,
    return_pct: float,
    trades_pnl: list[float],
) -> MultiStrategyResult:
    sr = StrategyResult(
        strategy_id="mr",
        display_name="Mean Reversion",
        initial_cash_usd=1000.0,
        final_cash_usd=1000.0 * (1 + return_pct / 100.0),
        trades=[_trade(p) for p in trades_pnl],
        total_trades=len(trades_pnl),
        wins=sum(1 for p in trades_pnl if p > 0),
        losses=sum(1 for p in trades_pnl if p < 0),
        total_pnl_usd=sum(trades_pnl),
        return_pct=return_pct,
        max_drawdown_pct=2.0,
        win_rate=(
            (sum(1 for p in trades_pnl if p > 0) / len(trades_pnl)) * 100.0
            if trades_pnl else 0.0
        ),
        profit_factor=(
            sum(p for p in trades_pnl if p > 0)
            / max(0.0001, -sum(p for p in trades_pnl if p < 0))
            if any(p < 0 for p in trades_pnl) else None
        ),
    )
    return MultiStrategyResult(
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        trading_days=21,
        strategies=[sr],
    )


# ---------------------------------------------------------------------------
# Bootstrap CI
# ---------------------------------------------------------------------------


class TestBootstrapCI:
    def test_returns_none_below_min_sample(self) -> None:
        ci = bootstrap_metric_ci(
            [0.01, 0.02], metric=metric_mean_return,
            metric_name="mean", samples=100, seed=1,
        )
        assert ci is None

    def test_lower_le_point_le_upper(self) -> None:
        returns = [0.02, -0.01, 0.03, -0.005, 0.015, 0.01, -0.02, 0.025]
        ci = bootstrap_metric_ci(
            returns, metric=metric_mean_return,
            metric_name="mean_return", samples=500, seed=42,
        )
        assert ci is not None
        assert ci.lower <= ci.point_estimate <= ci.upper

    def test_seed_is_reproducible(self) -> None:
        returns = [0.02, -0.01, 0.03, -0.005, 0.015, 0.01, -0.02, 0.025]
        ci1 = bootstrap_metric_ci(
            returns, metric=metric_profit_factor,
            metric_name="pf", samples=500, seed=7,
        )
        ci2 = bootstrap_metric_ci(
            returns, metric=metric_profit_factor,
            metric_name="pf", samples=500, seed=7,
        )
        assert ci1 == ci2

    def test_invalid_coverage_raises(self) -> None:
        with pytest.raises(ValueError):
            bootstrap_metric_ci(
                [0.01] * 10, metric=metric_mean_return,
                metric_name="m", samples=10, coverage=1.5, seed=0,
            )

    def test_profit_factor_handles_no_losses(self) -> None:
        # All-winners → +inf PF point estimate, no crash.
        returns = [0.01] * 10
        ci = bootstrap_metric_ci(
            returns, metric=metric_profit_factor,
            metric_name="pf", samples=200, seed=0,
        )
        assert ci is not None
        # Point estimate is +inf; bounds should also be +inf.
        assert ci.point_estimate == float("inf")


class TestWindowMetrics:
    def test_win_rate(self) -> None:
        assert metric_win_rate([0.01, -0.01, 0.02, 0.03]) == 0.75

    def test_profit_factor_basic(self) -> None:
        # gains = 0.05, losses = 0.02 → PF = 2.5
        assert metric_profit_factor([0.03, -0.02, 0.02]) == pytest.approx(2.5)

    def test_sharpe_approx_zero_when_constant(self) -> None:
        assert metric_sharpe_approx([0.01, 0.01, 0.01]) == 0.0


# ---------------------------------------------------------------------------
# run_walkforward
# ---------------------------------------------------------------------------


class TestRunWalkforward:
    @pytest.mark.asyncio
    async def test_splits_into_non_overlapping_windows(self) -> None:
        # 90-day windows over 270 days → 3 windows.
        d_from = date(2026, 1, 1)
        d_to = date(2026, 9, 27)  # 269 days

        captured: list[tuple[date, date]] = []

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            captured.append((d1, d2))
            return _multi_result(d1, d2, return_pct=2.0, trades_pnl=[10, -5, 15, -3, 8])

        cfg = WalkforwardConfig(window_days=90, step_days=90, bootstrap_samples=100)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)

        assert len(result.windows) == 3
        # First window starts at d_from.
        assert captured[0][0] == d_from
        # Windows are contiguous and 90 days each (last may be truncated).
        for d1, d2 in captured[:-1]:
            assert (d2 - d1).days == 89

    @pytest.mark.asyncio
    async def test_aggregate_stats_match_per_window(self) -> None:
        d_from = date(2026, 1, 1)
        d_to = date(2026, 6, 30)

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            return _multi_result(d1, d2, return_pct=3.0, trades_pnl=[12, -4, 8, -3, 10, -2])

        cfg = WalkforwardConfig(window_days=90, step_days=90, bootstrap_samples=200)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)

        assert "mr" in result.aggregate
        agg = result.aggregate["mr"]
        # Two windows × 6 trades = 12 OOS trades.
        assert agg["trades"] == 12
        # trades_pnl=[12,-4,8,-3,10,-2] → 3 wins, 3 losses per window.
        assert agg["wins"] == 6
        assert agg["losses"] == 6
        # OOS return compounds two +3% windows: (1.03)^2 - 1 = 6.09%.
        assert agg["return_pct"] == pytest.approx(6.09, rel=1e-3)

    @pytest.mark.asyncio
    async def test_bootstrap_cis_attached_for_each_strategy(self) -> None:
        d_from = date(2026, 1, 1)
        d_to = date(2026, 3, 31)

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            return _multi_result(d1, d2, return_pct=2.0,
                                 trades_pnl=[10, -3, 8, -2, 12, -4, 6])

        cfg = WalkforwardConfig(window_days=90, step_days=90,
                                bootstrap_samples=200, bootstrap_ci=0.90)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)

        cis = result.bootstrap.get("mr", {})
        assert "profit_factor" in cis
        assert "win_rate" in cis
        assert "mean_return" in cis
        assert "sharpe_approx" in cis
        for ci in cis.values():
            assert isinstance(ci, BootstrapCI)
            # Coverage was 0.90, so lower < upper is the only invariant we
            # can assert without making the test flaky.
            assert ci.lower <= ci.upper

    @pytest.mark.asyncio
    async def test_window_failure_is_skipped_not_fatal(self) -> None:
        d_from = date(2026, 1, 1)
        d_to = date(2026, 6, 30)
        call_count = {"n": 0}

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated data load failure")
            return _multi_result(d1, d2, return_pct=1.0, trades_pnl=[5, -2, 3, -1, 4])

        cfg = WalkforwardConfig(window_days=90, step_days=90, bootstrap_samples=100)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)

        # First window failed and was skipped; second was recorded.
        assert len(result.windows) == 1

    @pytest.mark.asyncio
    async def test_portfolio_aggregate_when_multi_strategy(self) -> None:
        """Multi-strategy run produces a synthetic _portfolio entry."""
        d_from = date(2026, 1, 1)
        d_to = date(2026, 6, 30)

        def _make_multi(d1: date, d2: date) -> MultiStrategyResult:
            r = _multi_result(d1, d2, return_pct=2.0,
                              trades_pnl=[8, -3, 6, -2, 5])
            r2 = _multi_result(d1, d2, return_pct=4.0,
                               trades_pnl=[12, -4, 10, -3, 8])
            r2.strategies[0].strategy_id = "breakout"
            r2.strategies[0].display_name = "Breakout"
            for t in r2.strategies[0].trades:
                t.strategy_id = "breakout"
            r.strategies.append(r2.strategies[0])
            return r

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            return _make_multi(d1, d2)

        cfg = WalkforwardConfig(window_days=90, step_days=90, bootstrap_samples=200)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)

        assert "_portfolio" in result.aggregate
        # 2 windows × 2 strategies × 5 trades = 20 pooled trades.
        assert result.aggregate["_portfolio"]["trades"] == 20
        # Portfolio has its own bootstrap CIs.
        assert "_portfolio" in result.bootstrap
        assert "profit_factor" in result.bootstrap["_portfolio"]

    @pytest.mark.asyncio
    async def test_no_portfolio_for_single_strategy(self) -> None:
        d_from = date(2026, 1, 1)
        d_to = date(2026, 6, 30)

        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            return _multi_result(d1, d2, return_pct=2.0,
                                 trades_pnl=[8, -3, 6, -2, 5])

        cfg = WalkforwardConfig(window_days=90, step_days=90, bootstrap_samples=200)
        result = await run_walkforward(d_from, d_to, runner, config=cfg)
        assert "_portfolio" not in result.aggregate

    @pytest.mark.asyncio
    async def test_invalid_window_days_raises(self) -> None:
        async def runner(d1: date, d2: date) -> MultiStrategyResult:
            return _multi_result(d1, d2, return_pct=0.0, trades_pnl=[])

        with pytest.raises(ValueError):
            await run_walkforward(
                date(2026, 1, 1), date(2026, 6, 30),
                runner,
                config=WalkforwardConfig(window_days=0, step_days=10),
            )

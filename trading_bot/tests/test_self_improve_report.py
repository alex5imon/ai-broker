"""Smoke tests for trading_bot.self_improve.report rendering."""

from __future__ import annotations

from datetime import date

import pytest

from trading_bot.self_improve.backtest_gate import BacktestComparison, StrategyMetrics
from trading_bot.self_improve.hypotheses import Proposal
from trading_bot.self_improve.postmortem import StrategyStats
from trading_bot.self_improve.report import render_markdown


def _stats(strategy_id: str, **kw) -> StrategyStats:
    defaults = dict(
        window_days=20,
        n_trades=30,
        win_rate=0.55,
        profit_factor=1.3,
        total_pnl_usd=42.0,
        avg_win_usd=6.0,
        avg_loss_usd=-4.0,
        avg_hold_minutes=45.0,
        exit_reason_counts={"take_profit": 10, "stop_loss": 8, "trailing_stop": 12},
    )
    defaults.update(kw)
    return StrategyStats(strategy_id=strategy_id, **defaults)


def _metrics(sid: str, **kw) -> StrategyMetrics:
    defaults = dict(
        n_trades=50, return_pct=5.0, max_drawdown_pct=4.0,
        win_rate=0.55, profit_factor=1.3, sharpe_approx=0.5, total_pnl_usd=125.0,
    )
    defaults.update(kw)
    return StrategyMetrics(strategy_id=sid, **defaults)


@pytest.mark.unit
def test_render_with_no_proposals_is_explicit():
    md = render_markdown(
        report_date=date(2026, 4, 30),
        window_days=20,
        stats_by_strategy={"mean_reversion": _stats("mean_reversion")},
        proposals=[],
        comparisons=[],
    )
    assert "Self-improvement review — 2026-04-30" in md
    assert "No rule fired" in md
    assert "mean_reversion" in md


@pytest.mark.unit
def test_render_with_passing_proposal_includes_patch_block():
    proposal = Proposal(
        rule_id="MR-RSI-TIGHTEN",
        strategy_id="mean_reversion",
        param_path=("rsi_oversold",),
        current_value=28,
        proposed_value=26,
        rationale="signal degraded",
        evidence={"n_trades": 30, "win_rate": 0.40},
    )
    comp = BacktestComparison(
        proposal=proposal,
        baseline=_metrics("mean_reversion", sharpe_approx=0.5, return_pct=5.0),
        candidate=_metrics("mean_reversion", sharpe_approx=0.6, return_pct=6.0),
        passed=True,
        reason="sharpe +0.10, drawdown +0.00pp, return +1.00pp",
    )
    md = render_markdown(
        report_date=date(2026, 4, 30),
        window_days=20,
        stats_by_strategy={"mean_reversion": _stats("mean_reversion")},
        proposals=[proposal],
        comparisons=[comp],
        backtest_window=(date(2026, 1, 1), date(2026, 4, 1)),
        backtest_universe=["SPY", "QQQ"],
    )
    assert "[PASS]" in md
    assert "Suggested patch" in md
    assert "rsi_oversold: 26" in md
    assert "was 28" in md
    assert "Backtest window: 2026-01-01 to 2026-04-01" in md
    assert "SPY, QQQ" in md


@pytest.mark.unit
def test_render_with_failing_proposal_omits_patch_block():
    proposal = Proposal(
        rule_id="MR-RSI-TIGHTEN",
        strategy_id="mean_reversion",
        param_path=("rsi_oversold",),
        current_value=28,
        proposed_value=26,
        rationale="signal degraded",
        evidence={"n_trades": 30},
    )
    comp = BacktestComparison(
        proposal=proposal,
        baseline=_metrics("mean_reversion", sharpe_approx=0.6),
        candidate=_metrics("mean_reversion", sharpe_approx=0.3),
        passed=False,
        reason="Sharpe dropped -0.30",
    )
    md = render_markdown(
        report_date=date(2026, 4, 30),
        window_days=20,
        stats_by_strategy={"mean_reversion": _stats("mean_reversion")},
        proposals=[proposal],
        comparisons=[comp],
    )
    assert "[FAIL]" in md
    assert "Suggested patch" not in md


@pytest.mark.unit
def test_render_handles_zero_trade_strategy():
    md = render_markdown(
        report_date=date(2026, 4, 30),
        window_days=20,
        stats_by_strategy={
            "overnight_drift": _stats(
                "overnight_drift",
                n_trades=0,
                win_rate=0.0,
                profit_factor=None,
                total_pnl_usd=0.0,
                avg_win_usd=0.0,
                avg_loss_usd=0.0,
                avg_hold_minutes=0.0,
                exit_reason_counts={},
            ),
        },
        proposals=[],
        comparisons=[],
    )
    assert "no closed trades in window" in md
    assert "n/a" in md  # PF rendered as n/a

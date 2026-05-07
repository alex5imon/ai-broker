"""Tests for trading_bot.self_improve.backtest_gate.

Uses a stub runner so we don't actually invoke the real backtester. The
real backtester is exercised by the existing test_multi_strategy_backtest
suite — here we only test the A/B harness.
"""

from __future__ import annotations


import pytest

from trading_bot.config import Config
from trading_bot.multi_strategy_backtest import (
    MultiStrategyResult,
    StrategyResult,
)
from trading_bot.self_improve.backtest_gate import (
    StrategyMetrics,
    _judge,
    _mutate_config,
    evaluate,
)
from trading_bot.self_improve.hypotheses import Proposal


def _make_proposal(
    strategy_id: str = "mean_reversion",
    param_path: tuple[str, ...] = ("rsi_oversold",),
    current: float = 28,
    proposed: float = 26,
) -> Proposal:
    return Proposal(
        rule_id="TEST-RULE",
        strategy_id=strategy_id,
        param_path=param_path,
        current_value=current,
        proposed_value=proposed,
        rationale="test",
        evidence={},
    )


def _make_strategy_result(
    strategy_id: str,
    *,
    return_pct: float = 5.0,
    max_drawdown_pct: float = 4.0,
    sharpe_approx: float = 0.5,
    n_trades: int = 50,
    win_rate: float = 0.55,
    profit_factor: float | None = 1.3,
) -> StrategyResult:
    return StrategyResult(
        strategy_id=strategy_id,
        display_name=strategy_id,
        initial_cash_usd=2500.0,
        final_cash_usd=2500.0 * (1 + return_pct / 100),
        total_trades=n_trades,
        wins=int(n_trades * win_rate),
        losses=n_trades - int(n_trades * win_rate),
        total_pnl_usd=2500.0 * return_pct / 100,
        return_pct=return_pct,
        max_drawdown_pct=max_drawdown_pct,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_hold_minutes=45.0,
        sharpe_approx=sharpe_approx,
    )


def _result(strategy_results: list[StrategyResult]) -> MultiStrategyResult:
    return MultiStrategyResult(
        from_date="2026-01-01",
        to_date="2026-04-01",
        trading_days=60,
        strategies=strategy_results,
    )


# ---------------------------------------------------------------------------
# _mutate_config
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_mutate_config_preserves_other_keys():
    base = {
        "trading": {
            "strategies": {
                "mean_reversion": {"enabled": True, "rsi_oversold": 28, "atr_stop_mult": 2.0},
                "overnight_drift": {"enabled": True, "stop_loss_pct": 0.03},
            }
        }
    }
    new = _mutate_config(base, "mean_reversion", ("rsi_oversold",), 26)
    assert new["trading"]["strategies"]["mean_reversion"]["rsi_oversold"] == 26
    assert new["trading"]["strategies"]["mean_reversion"]["atr_stop_mult"] == 2.0
    assert new["trading"]["strategies"]["overnight_drift"]["stop_loss_pct"] == 0.03
    # original is not mutated
    assert base["trading"]["strategies"]["mean_reversion"]["rsi_oversold"] == 28


@pytest.mark.unit
def test_mutate_config_preserves_int_type():
    base = {"trading": {"strategies": {"mean_reversion": {"rsi_oversold": 28}}}}
    new = _mutate_config(base, "mean_reversion", ("rsi_oversold",), 26.0)
    assert isinstance(new["trading"]["strategies"]["mean_reversion"]["rsi_oversold"], int)


@pytest.mark.unit
def test_mutate_config_unknown_strategy_raises():
    base = {"trading": {"strategies": {"mean_reversion": {"rsi_oversold": 28}}}}
    with pytest.raises(KeyError):
        _mutate_config(base, "nonexistent", ("rsi_oversold",), 26)


@pytest.mark.unit
def test_mutate_config_refuses_to_create_new_key():
    base = {"trading": {"strategies": {"mean_reversion": {"rsi_oversold": 28}}}}
    with pytest.raises(KeyError):
        _mutate_config(base, "mean_reversion", ("nonexistent_param",), 5)


# ---------------------------------------------------------------------------
# _judge
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_judge_passes_when_candidate_matches_baseline():
    baseline = StrategyMetrics.from_result(_make_strategy_result("mr"))
    candidate = StrategyMetrics.from_result(_make_strategy_result("mr"))
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert passed
    assert "sharpe" in reason


@pytest.mark.unit
def test_judge_fails_on_sharpe_drop():
    baseline = StrategyMetrics.from_result(_make_strategy_result("mr", sharpe_approx=0.6))
    candidate = StrategyMetrics.from_result(_make_strategy_result("mr", sharpe_approx=0.4))
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert not passed
    assert "Sharpe" in reason


@pytest.mark.unit
def test_judge_fails_on_drawdown_increase():
    baseline = StrategyMetrics.from_result(_make_strategy_result("mr", max_drawdown_pct=4.0))
    candidate = StrategyMetrics.from_result(_make_strategy_result("mr", max_drawdown_pct=7.0))
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert not passed
    assert "drawdown" in reason.lower()


@pytest.mark.unit
def test_judge_fails_on_return_drop():
    baseline = StrategyMetrics.from_result(_make_strategy_result("mr", return_pct=10.0))
    candidate = StrategyMetrics.from_result(_make_strategy_result("mr", return_pct=5.0))
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert not passed
    assert "Return" in reason


@pytest.mark.unit
def test_judge_fails_on_zero_trades():
    baseline = StrategyMetrics.from_result(_make_strategy_result("mr"))
    candidate = StrategyMetrics.from_result(_make_strategy_result("mr", n_trades=0))
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert not passed
    assert "zero trades" in reason


@pytest.mark.unit
def test_judge_fails_on_negative_baseline_with_much_worse_candidate():
    """Regression for review HIGH (backtest_gate negative-baseline
    bypass): pre-fix, the return-ratio check was skipped whenever
    baseline.return_pct <= 0, so a -50% candidate could pass against a
    -0.01% baseline. With both at negative returns, candidate must not
    be more negative than baseline by more than (1-MIN_RETURN_RATIO)
    of |baseline|.
    """
    baseline = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=-1.0)
    )
    # Candidate is -50% — catastrophically worse than -1% baseline.
    candidate = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=-50.0)
    )
    passed, reason = _judge(_make_proposal(), baseline, candidate)
    assert not passed, (
        "negative-baseline bypass regression: a -50% candidate must "
        "fail against a -1% baseline"
    )
    assert "Return" in reason or "loss" in reason.lower()


@pytest.mark.unit
def test_judge_passes_on_negative_baseline_with_marginal_candidate():
    """Symmetric check: a candidate within tolerance of a negative
    baseline must still pass."""
    baseline = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=-1.0)
    )
    # Candidate at -1.04% — within 5% extra drawdown of |baseline|.
    candidate = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=-1.04)
    )
    passed, _ = _judge(_make_proposal(), baseline, candidate)
    assert passed


@pytest.mark.unit
def test_judge_passes_on_zero_baseline_when_candidate_is_neutral():
    """Zero baseline: ratio is undefined. Sharpe + drawdown gates are
    the only quality controls. A candidate matching baseline metrics
    must pass."""
    baseline = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=0.0)
    )
    candidate = StrategyMetrics.from_result(
        _make_strategy_result("mr", return_pct=0.0)
    )
    passed, _ = _judge(_make_proposal(), baseline, candidate)
    assert passed


# ---------------------------------------------------------------------------
# evaluate (end-to-end with stub runner)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_runs_baseline_once_per_call():
    base_raw = {
        "trading": {
            "strategies": {
                "mean_reversion": {"enabled": True, "rsi_oversold": 28},
            }
        }
    }
    config = Config(base_raw)

    call_count = {"n": 0}
    seen_rsi: list[int] = []

    async def stub_runner(c: Config) -> MultiStrategyResult:
        call_count["n"] += 1
        rsi = c._raw["trading"]["strategies"]["mean_reversion"]["rsi_oversold"]
        seen_rsi.append(rsi)
        # Improving candidate
        sharpe = 0.5 if rsi == 28 else 0.6
        return _result([_make_strategy_result("mean_reversion", sharpe_approx=sharpe)])

    proposals = [
        _make_proposal(current=28, proposed=26),
        _make_proposal(param_path=("rsi_recovery",), current=35, proposed=33),
    ]
    # Add the second param path to the config so mutation succeeds
    base_raw["trading"]["strategies"]["mean_reversion"]["rsi_recovery"] = 35

    comps = await evaluate(proposals, config, stub_runner)
    # 1 baseline + 2 candidates = 3 runs
    assert call_count["n"] == 3
    assert seen_rsi[0] == 28  # baseline first
    assert all(c.passed for c in comps)
    assert len(comps) == 2


@pytest.mark.asyncio
async def test_evaluate_skips_proposal_when_strategy_not_in_baseline():
    base_raw = {
        "trading": {
            "strategies": {"mean_reversion": {"enabled": True, "rsi_oversold": 28}}
        }
    }
    config = Config(base_raw)

    async def stub_runner(c: Config) -> MultiStrategyResult:
        return _result([_make_strategy_result("mean_reversion")])

    # Proposal targets a strategy that isn't in baseline output
    proposal = _make_proposal(strategy_id="ghost", current=1, proposed=2)
    comps = await evaluate([proposal], config, stub_runner)
    assert comps == []


@pytest.mark.asyncio
async def test_evaluate_empty_proposals_returns_empty():
    config = Config({"trading": {"strategies": {}}})

    async def stub_runner(c: Config) -> MultiStrategyResult:
        raise AssertionError("runner should not be called when no proposals")

    assert await evaluate([], config, stub_runner) == []


@pytest.mark.asyncio
async def test_evaluate_marks_failing_candidate():
    base_raw = {
        "trading": {
            "strategies": {"mean_reversion": {"enabled": True, "rsi_oversold": 28}}
        }
    }
    config = Config(base_raw)

    async def stub_runner(c: Config) -> MultiStrategyResult:
        rsi = c._raw["trading"]["strategies"]["mean_reversion"]["rsi_oversold"]
        # Worse candidate
        sharpe = 0.6 if rsi == 28 else 0.3
        return _result([_make_strategy_result("mean_reversion", sharpe_approx=sharpe)])

    comps = await evaluate(
        [_make_proposal(current=28, proposed=26)], config, stub_runner,
    )
    assert len(comps) == 1
    assert not comps[0].passed

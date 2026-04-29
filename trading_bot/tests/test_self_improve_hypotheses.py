"""Tests for trading_bot.self_improve.hypotheses."""

from __future__ import annotations

import pytest

from trading_bot.self_improve.hypotheses import (
    BASELINES,
    MAX_PROPOSALS_PER_RUN,
    MIN_TRADES_FOR_PROPOSAL,
    propose,
)
from trading_bot.self_improve.postmortem import StrategyStats


def _stats(
    strategy_id: str,
    *,
    n_trades: int = 30,
    win_rate: float = 0.55,
    profit_factor: float | None = 1.3,
    total_pnl_usd: float = 50.0,
    avg_win_usd: float = 6.0,
    avg_loss_usd: float = -4.0,
    avg_hold_minutes: float = 90.0,
    exit_reason_counts: dict[str, int] | None = None,
) -> StrategyStats:
    return StrategyStats(
        strategy_id=strategy_id,
        window_days=20,
        n_trades=n_trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl_usd=total_pnl_usd,
        avg_win_usd=avg_win_usd,
        avg_loss_usd=avg_loss_usd,
        avg_hold_minutes=avg_hold_minutes,
        exit_reason_counts=exit_reason_counts or {},
    )


# Realistic config snippets matching config.yaml shape.
MR_CFG = {
    "enabled": True,
    "rsi_oversold": 28,
    "atr_stop_mult": 2.0,
    "let_winners_run": True,
    "let_winners_run_up_pct": 0.03,
}

OD_CFG = {
    "enabled": True,
    "stop_loss_pct": 0.03,
}

CONFIGS = {"mean_reversion": MR_CFG, "overnight_drift": OD_CFG}


@pytest.mark.unit
def test_no_proposal_when_within_baseline():
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            win_rate=0.55,
            profit_factor=1.3,
            exit_reason_counts={"take_profit": 12, "stop_loss": 10, "trailing_stop": 8},
        ),
        "overnight_drift": _stats(
            "overnight_drift",
            win_rate=0.6,
            profit_factor=1.1,
            # 3/30 = 10% stop_loss share, well below 15% trigger
            exit_reason_counts={"take_profit": 27, "stop_loss": 3},
        ),
    }
    assert propose(stats, CONFIGS) == []


@pytest.mark.unit
def test_no_proposal_below_min_trades():
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            n_trades=MIN_TRADES_FOR_PROPOSAL - 1,
            win_rate=0.10,
            profit_factor=0.5,
            exit_reason_counts={"stop_loss": 19},
        ),
    }
    assert propose(stats, CONFIGS) == []


@pytest.mark.unit
def test_mr_rsi_tighten_fires_on_degraded_signal():
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            n_trades=30,
            # baseline WR=0.527, baseline PF=1.201
            win_rate=0.40,         # 12.7pp below baseline
            profit_factor=0.95,    # 79% of baseline (< 0.85)
            exit_reason_counts={"stop_loss": 10, "take_profit": 8, "trailing_stop": 12},
        ),
    }
    out = propose(stats, CONFIGS)
    rule_ids = [p.rule_id for p in out]
    assert "MR-RSI-TIGHTEN" in rule_ids
    p = next(p for p in out if p.rule_id == "MR-RSI-TIGHTEN")
    assert p.param_path == ("rsi_oversold",)
    assert p.current_value == 28
    assert p.proposed_value == 26
    assert p.proposed_value < p.current_value


@pytest.mark.unit
def test_mr_atr_tighten_fires_on_high_stop_share():
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            n_trades=30,
            win_rate=0.50,
            profit_factor=1.1,
            exit_reason_counts={"stop_loss": 18, "take_profit": 6, "trailing_stop": 6},
        ),
    }
    out = propose(stats, CONFIGS)
    p = next(p for p in out if p.rule_id == "MR-ATR-TIGHTEN")
    assert p.param_path == ("atr_stop_mult",)
    assert p.proposed_value == 1.8


@pytest.mark.unit
def test_mr_lwr_earlier_fires_when_trail_never_engages():
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            n_trades=30,
            win_rate=0.55,
            profit_factor=1.2,
            avg_hold_minutes=40,
            exit_reason_counts={"stop_loss": 8, "wind_down": 22},
        ),
    }
    out = propose(stats, CONFIGS)
    p = next(p for p in out if p.rule_id == "MR-LWR-EARLIER")
    assert p.param_path == ("let_winners_run_up_pct",)
    assert p.proposed_value < p.current_value
    assert p.proposed_value == pytest.approx(0.024, abs=1e-4)


@pytest.mark.unit
def test_od_stop_tighten_fires_on_high_overnight_stop_share():
    stats = {
        "overnight_drift": _stats(
            "overnight_drift",
            n_trades=40,
            win_rate=0.55,
            profit_factor=1.0,
            exit_reason_counts={"stop_loss": 8, "wind_down": 32},
        ),
    }
    out = propose(stats, CONFIGS)
    p = next(p for p in out if p.rule_id == "OD-STOP-TIGHTEN")
    assert p.param_path == ("stop_loss_pct",)
    assert p.proposed_value == pytest.approx(0.027, abs=1e-4)


@pytest.mark.unit
def test_proposals_capped():
    """Many failure modes simultaneously — output is bounded."""
    stats = {
        "mean_reversion": _stats(
            "mean_reversion",
            n_trades=30,
            win_rate=0.40,
            profit_factor=0.95,
            avg_hold_minutes=40,
            exit_reason_counts={"stop_loss": 22, "wind_down": 8},
        ),
        "overnight_drift": _stats(
            "overnight_drift",
            n_trades=40,
            win_rate=0.55,
            profit_factor=1.0,
            exit_reason_counts={"stop_loss": 10, "wind_down": 30},
        ),
    }
    out = propose(stats, CONFIGS)
    assert len(out) <= MAX_PROPOSALS_PER_RUN


@pytest.mark.unit
def test_unknown_strategy_skipped():
    stats = {
        "trend_following": _stats(
            "trend_following",
            n_trades=30,
            win_rate=0.10,
            profit_factor=0.3,
            exit_reason_counts={"stop_loss": 27},
        ),
    }
    assert propose(stats, {"trend_following": {"stop_loss_pct": 0.03}}) == []


@pytest.mark.unit
def test_baselines_are_documented_for_active_strategies():
    """Guards against silently dropping a baseline for an enabled strategy."""
    assert "mean_reversion" in BASELINES
    assert "overnight_drift" in BASELINES
    for sid, b in BASELINES.items():
        assert "win_rate" in b
        assert "profit_factor" in b
        assert 0 < b["win_rate"] < 1
        assert b["profit_factor"] > 0

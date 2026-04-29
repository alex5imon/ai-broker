"""Rule-based hypothesis generator.

Each rule is deterministic, requires a minimum evidence threshold, and
produces a single bounded parameter step. Rules are written defensively:
they only fire on patterns the human reviewer can verify in the postmortem
table, and they only touch parameters that the validated strategies expose.

The agent never proposes changes to safety-critical parameters (risk gates,
circuit breakers, position sizing). Proposals are scoped to per-strategy
signal/exit thresholds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from trading_bot.self_improve.postmortem import StrategyStats

logger = logging.getLogger(__name__)


# Validated baselines (see config.yaml comments + tune_history memory).
# Used only to detect material regressions, never to drive auto-tuning to
# match a target.
BASELINES: dict[str, dict[str, float]] = {
    "mean_reversion": {"win_rate": 0.527, "profit_factor": 1.201},
    "overnight_drift": {"win_rate": 0.55, "profit_factor": 1.052},
}

# Minimum number of closed trades in the window before any rule may fire.
# Below this, the noise on win-rate / PF is too large to act on.
MIN_TRADES_FOR_PROPOSAL: int = 20

# Maximum number of proposals to surface in a single report. Keeps each
# day's review focused; avoids overwhelming the human reviewer.
MAX_PROPOSALS_PER_RUN: int = 3


@dataclass(frozen=True)
class Proposal:
    """A single proposed config change.

    ``param_path`` is the YAML key path under
    ``trading.strategies.<strategy_id>``, e.g. ``("rsi_oversold",)`` or
    ``("atr_stop_mult",)``. ``current_value`` and ``proposed_value`` are
    plain Python scalars (int or float).
    """

    rule_id: str
    strategy_id: str
    param_path: tuple[str, ...]
    current_value: float
    proposed_value: float
    rationale: str
    evidence: dict[str, float | int | str]


Rule = Callable[
    [StrategyStats, dict[str, Any], dict[str, float]],
    Proposal | None,
]


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _rule_mr_rsi_tighten(
    stats: StrategyStats,
    cfg: dict[str, Any],
    baseline: dict[str, float],
) -> Proposal | None:
    if stats.strategy_id != "mean_reversion":
        return None
    if stats.n_trades < MIN_TRADES_FOR_PROPOSAL:
        return None
    pf = stats.profit_factor
    if pf is None:
        return None
    wr_drop = baseline["win_rate"] - stats.win_rate
    pf_ratio = pf / baseline["profit_factor"]
    if wr_drop < 0.05 or pf_ratio >= 0.85:
        return None

    current = int(cfg.get("rsi_oversold", 28))
    proposed = max(20, current - 2)
    if proposed == current:
        return None

    return Proposal(
        rule_id="MR-RSI-TIGHTEN",
        strategy_id=stats.strategy_id,
        param_path=("rsi_oversold",),
        current_value=float(current),
        proposed_value=float(proposed),
        rationale=(
            f"Win rate {stats.win_rate:.1%} is {wr_drop * 100:.1f}pp below baseline "
            f"({baseline['win_rate']:.1%}) and PF {pf:.2f} is {(1 - pf_ratio) * 100:.0f}% "
            f"below baseline ({baseline['profit_factor']:.2f}). Signal quality has "
            f"degraded — tighten RSI oversold threshold to require deeper dips."
        ),
        evidence={
            "n_trades": stats.n_trades,
            "win_rate": round(stats.win_rate, 4),
            "profit_factor": round(pf, 4),
            "baseline_win_rate": baseline["win_rate"],
            "baseline_profit_factor": baseline["profit_factor"],
        },
    )


def _rule_mr_atr_tighten(
    stats: StrategyStats,
    cfg: dict[str, Any],
    baseline: dict[str, float],
) -> Proposal | None:
    if stats.strategy_id != "mean_reversion":
        return None
    if stats.n_trades < MIN_TRADES_FOR_PROPOSAL:
        return None
    stop_share = stats.exit_share("stop_loss")
    if stop_share <= 0.55:
        return None

    current = float(cfg.get("atr_stop_mult", 2.0))
    proposed = round(current * 0.9, 2)
    if proposed >= current or proposed < 1.0:
        return None

    return Proposal(
        rule_id="MR-ATR-TIGHTEN",
        strategy_id=stats.strategy_id,
        param_path=("atr_stop_mult",),
        current_value=current,
        proposed_value=proposed,
        rationale=(
            f"{stop_share:.0%} of trades exited at stop_loss (>55% threshold). "
            f"Stops are absorbing trades that drift before recovering — tighten ATR "
            f"stop multiplier 10% to cut average loss size."
        ),
        evidence={
            "n_trades": stats.n_trades,
            "stop_loss_share": round(stop_share, 4),
            "avg_loss_usd": round(stats.avg_loss_usd, 2),
            "avg_win_usd": round(stats.avg_win_usd, 2),
        },
    )


def _rule_mr_lwr_earlier(
    stats: StrategyStats,
    cfg: dict[str, Any],
    baseline: dict[str, float],
) -> Proposal | None:
    if stats.strategy_id != "mean_reversion":
        return None
    if stats.n_trades < MIN_TRADES_FOR_PROPOSAL:
        return None
    if not cfg.get("let_winners_run", False):
        return None
    take_profit_share = stats.exit_share("take_profit")
    trailing_share = stats.exit_share("trailing_stop")
    if take_profit_share + trailing_share > 0.20:
        return None
    if stats.avg_hold_minutes >= 60:
        return None

    current = float(cfg.get("let_winners_run_up_pct", 0.03))
    proposed = round(current * 0.8, 4)
    if proposed >= current or proposed < 0.005:
        return None

    return Proposal(
        rule_id="MR-LWR-EARLIER",
        strategy_id=stats.strategy_id,
        param_path=("let_winners_run_up_pct",),
        current_value=current,
        proposed_value=proposed,
        rationale=(
            f"let_winners_run is on but only {(take_profit_share + trailing_share):.0%} "
            f"of exits engaged the trailing stop, and avg hold is "
            f"{stats.avg_hold_minutes:.0f}min. Lower the activation threshold so "
            f"the trail engages earlier in winning trades."
        ),
        evidence={
            "n_trades": stats.n_trades,
            "take_profit_share": round(take_profit_share, 4),
            "trailing_stop_share": round(trailing_share, 4),
            "avg_hold_minutes": round(stats.avg_hold_minutes, 1),
        },
    )


def _rule_od_stop_tighten(
    stats: StrategyStats,
    cfg: dict[str, Any],
    baseline: dict[str, float],
) -> Proposal | None:
    if stats.strategy_id != "overnight_drift":
        return None
    if stats.n_trades < MIN_TRADES_FOR_PROPOSAL:
        return None
    stop_share = stats.exit_share("stop_loss")
    # Overnight drift's natural stop_loss share should be tiny (~5% of nights
    # see >3% gap-downs). Anything above 15% suggests the disaster stop is
    # eating more trades than the strategy archetype tolerates.
    if stop_share <= 0.15:
        return None

    current = float(cfg.get("stop_loss_pct", 0.03))
    proposed = round(current * 0.9, 4)
    if proposed >= current or proposed < 0.01:
        return None

    return Proposal(
        rule_id="OD-STOP-TIGHTEN",
        strategy_id=stats.strategy_id,
        param_path=("stop_loss_pct",),
        current_value=current,
        proposed_value=proposed,
        rationale=(
            f"{stop_share:.0%} of overnight trades exited at the disaster stop. "
            f"Tail risk is materializing more often than the archetype expects — "
            f"tighten the stop 10% to cap individual gap-down losses."
        ),
        evidence={
            "n_trades": stats.n_trades,
            "stop_loss_share": round(stop_share, 4),
            "avg_loss_usd": round(stats.avg_loss_usd, 2),
        },
    )


ALL_RULES: tuple[Rule, ...] = (
    _rule_mr_rsi_tighten,
    _rule_mr_atr_tighten,
    _rule_mr_lwr_earlier,
    _rule_od_stop_tighten,
)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def propose(
    stats_by_strategy: dict[str, StrategyStats],
    strategy_configs: dict[str, dict[str, Any]],
    *,
    baselines: dict[str, dict[str, float]] | None = None,
    rules: tuple[Rule, ...] | None = None,
    max_proposals: int = MAX_PROPOSALS_PER_RUN,
) -> list[Proposal]:
    """Run all rules, return up to ``max_proposals`` candidates.

    Returns an empty list if no rule fires — the report should make this
    explicit rather than fabricating proposals.
    """
    baselines_used = baselines or BASELINES
    rules_used = rules or ALL_RULES

    proposals: list[Proposal] = []
    for sid, stats in stats_by_strategy.items():
        cfg = strategy_configs.get(sid, {})
        baseline = baselines_used.get(sid)
        if baseline is None:
            logger.debug("No baseline for %s — skipping rules", sid)
            continue
        for rule in rules_used:
            try:
                p = rule(stats, cfg, baseline)
            except Exception:
                logger.exception("Rule %s raised on %s", rule.__name__, sid)
                continue
            if p is not None:
                proposals.append(p)

    if len(proposals) > max_proposals:
        # Prefer proposals from strategies with the most trades (more signal).
        proposals.sort(
            key=lambda p: stats_by_strategy[p.strategy_id].n_trades,
            reverse=True,
        )
        proposals = proposals[:max_proposals]

    logger.info("Generated %d proposals", len(proposals))
    return proposals

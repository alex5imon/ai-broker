"""Backtest gate: validate each proposal against historical data.

Runs the existing ``MultiStrategyBacktester`` in-process. For each proposal:
  1. Builds a candidate ``Config`` with the single parameter mutated.
  2. Runs both baseline and candidate over the same window.
  3. Compares ``StrategyResult`` for the affected strategy.
  4. Marks the proposal pass/fail based on relative-regression thresholds.

The comparison is intentionally strict on regression (no Sharpe drop,
no material drawdown increase) and only mildly demanding on improvement
(the candidate must at least not be worse). The agent surfaces all
results — pass and fail — and the human picks.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any, Awaitable, Callable

from trading_bot.config import Config
from trading_bot.multi_strategy_backtest import (
    MultiStrategyBacktester,
    MultiStrategyResult,
    StrategyResult,
)
from trading_bot.self_improve.hypotheses import Proposal

logger = logging.getLogger(__name__)


# Pass criteria (strict on regression, lenient on improvement). All must hold
# for a proposal to be marked passed.
MAX_SHARPE_DROP: float = 0.10
MAX_DRAWDOWN_INCREASE_PCT: float = 2.0     # absolute percentage points
MIN_RETURN_RATIO: float = 0.95              # candidate >= 95% of baseline return


@dataclass(frozen=True)
class StrategyMetrics:
    """Subset of ``StrategyResult`` we use for A/B comparison."""

    strategy_id: str
    n_trades: int
    return_pct: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float | None
    sharpe_approx: float
    total_pnl_usd: float

    @classmethod
    def from_result(cls, r: StrategyResult) -> StrategyMetrics:
        return cls(
            strategy_id=r.strategy_id,
            n_trades=r.total_trades,
            return_pct=r.return_pct,
            max_drawdown_pct=r.max_drawdown_pct,
            win_rate=r.win_rate,
            profit_factor=r.profit_factor,
            sharpe_approx=r.sharpe_approx,
            total_pnl_usd=r.total_pnl_usd,
        )


@dataclass(frozen=True)
class BacktestComparison:
    """Result of validating a single proposal against historical data."""

    proposal: Proposal
    baseline: StrategyMetrics
    candidate: StrategyMetrics
    passed: bool
    reason: str


# A backtest runner: takes a Config and returns a MultiStrategyResult. The
# CLI passes one bound to the chosen mode (multi-intraday / spy / daily)
# and date window. Tests can supply a stub.
BacktestRunner = Callable[[Config], Awaitable[MultiStrategyResult]]


def _mutate_config(
    base_raw: dict[str, Any],
    strategy_id: str,
    param_path: tuple[str, ...],
    new_value: float,
) -> dict[str, Any]:
    """Return a deep-copy of ``base_raw`` with one strategy param replaced."""
    new_raw = copy.deepcopy(base_raw)
    strategies = new_raw.get("trading", {}).get("strategies", {})
    if strategy_id not in strategies:
        raise KeyError(
            f"Strategy {strategy_id} not found in config "
            f"(have: {list(strategies.keys())})"
        )
    target = strategies[strategy_id]
    for key in param_path[:-1]:
        if key not in target:
            raise KeyError(
                f"Param path {'.'.join(param_path)} broken at '{key}' "
                f"under {strategy_id}"
            )
        target = target[key]
    leaf = param_path[-1]
    if leaf not in target:
        raise KeyError(
            f"Leaf param '{leaf}' not present under {strategy_id}; "
            f"refusing to add a new key"
        )
    # Preserve int-ness where possible (RSI thresholds are ints in YAML).
    if isinstance(target[leaf], int) and float(new_value).is_integer():
        target[leaf] = int(new_value)
    else:
        target[leaf] = new_value
    return new_raw


def _judge(
    proposal: Proposal,
    baseline: StrategyMetrics,
    candidate: StrategyMetrics,
) -> tuple[bool, str]:
    """Return (passed, human-readable reason)."""
    notes: list[str] = []
    failed = False

    sharpe_delta = candidate.sharpe_approx - baseline.sharpe_approx
    if sharpe_delta < -MAX_SHARPE_DROP:
        failed = True
        notes.append(
            f"Sharpe dropped {sharpe_delta:+.2f} (max allowed {-MAX_SHARPE_DROP:+.2f})"
        )

    dd_delta = candidate.max_drawdown_pct - baseline.max_drawdown_pct
    if dd_delta > MAX_DRAWDOWN_INCREASE_PCT:
        failed = True
        notes.append(
            f"Max drawdown worsened by {dd_delta:+.2f}pp "
            f"(max allowed {MAX_DRAWDOWN_INCREASE_PCT:+.2f}pp)"
        )

    if baseline.return_pct > 0:
        ratio = candidate.return_pct / baseline.return_pct
        if ratio < MIN_RETURN_RATIO:
            failed = True
            notes.append(
                f"Return dropped to {ratio:.0%} of baseline "
                f"(min {MIN_RETURN_RATIO:.0%})"
            )

    if candidate.n_trades == 0:
        failed = True
        notes.append("Candidate produced zero trades over the window")

    if failed:
        return False, "; ".join(notes)
    return True, (
        f"sharpe {sharpe_delta:+.2f}, drawdown {dd_delta:+.2f}pp, "
        f"return {(candidate.return_pct - baseline.return_pct):+.2f}pp"
    )


async def evaluate(
    proposals: list[Proposal],
    base_config: Config,
    runner: BacktestRunner,
) -> list[BacktestComparison]:
    """Run baseline + per-proposal candidates, return judged comparisons.

    The baseline is run once and reused across all proposals — they share
    the same window and starting state, so the baseline metrics are
    invariant.
    """
    if not proposals:
        return []

    logger.info("Running baseline backtest...")
    baseline_result = await runner(base_config)
    baseline_by_sid: dict[str, StrategyMetrics] = {
        r.strategy_id: StrategyMetrics.from_result(r)
        for r in baseline_result.strategies
    }

    comparisons: list[BacktestComparison] = []
    for proposal in proposals:
        if proposal.strategy_id not in baseline_by_sid:
            logger.warning(
                "Baseline did not include strategy %s — skipping proposal %s",
                proposal.strategy_id, proposal.rule_id,
            )
            continue

        try:
            mutated_raw = _mutate_config(
                base_config._raw,
                proposal.strategy_id,
                proposal.param_path,
                proposal.proposed_value,
            )
        except KeyError as exc:
            logger.error("Cannot apply proposal %s: %s", proposal.rule_id, exc)
            continue

        candidate_config = Config(mutated_raw)
        logger.info(
            "Running candidate for %s (%s: %s -> %s)",
            proposal.rule_id, ".".join(proposal.param_path),
            proposal.current_value, proposal.proposed_value,
        )
        candidate_result = await runner(candidate_config)
        candidate_metrics_by_sid = {
            r.strategy_id: StrategyMetrics.from_result(r)
            for r in candidate_result.strategies
        }
        if proposal.strategy_id not in candidate_metrics_by_sid:
            logger.warning(
                "Candidate did not produce a result for %s — skipping",
                proposal.strategy_id,
            )
            continue

        baseline = baseline_by_sid[proposal.strategy_id]
        candidate = candidate_metrics_by_sid[proposal.strategy_id]
        passed, reason = _judge(proposal, baseline, candidate)
        comparisons.append(
            BacktestComparison(
                proposal=proposal,
                baseline=baseline,
                candidate=candidate,
                passed=passed,
                reason=reason,
            )
        )

    n_passed = sum(1 for c in comparisons if c.passed)
    logger.info("%d/%d proposals passed the backtest gate", n_passed, len(comparisons))
    return comparisons


# Convenience adapter for the multi-ticker intraday mode the live bot uses.
def make_multi_intraday_runner(
    tickers: list[str],
    from_date: date,
    to_date: date,
    *,
    cash_per_strategy_usd: float = 2500.0,
    regime_filter: bool = True,
) -> BacktestRunner:
    """Build a runner bound to multi-ticker intraday mode + date window."""

    async def _runner(config: Config) -> MultiStrategyResult:
        engine = MultiStrategyBacktester(config)
        return await engine.run_multi_ticker_intraday(
            from_date,
            to_date,
            tickers=tickers,
            cash_per_strategy_usd=cash_per_strategy_usd,
            regime_filter=regime_filter,
        )

    return _runner

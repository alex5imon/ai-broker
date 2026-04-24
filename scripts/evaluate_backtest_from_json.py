#!/usr/bin/env python3
"""Run backtest-expert evaluation over a multi_strategy backtest result file.

Reads one of ``backtest_results/multi_strategy_*.json`` (or
``backtest_results/config_*.json``), derives the per-strategy metrics the
evaluator expects, and writes one eval report per strategy to
``reports/``.  The underlying scoring comes from the vendored skill at
``.claude/skills/backtest-expert/scripts/evaluate_backtest.py``.

Usage
-----

::

    python scripts/evaluate_backtest_from_json.py \\
        backtest_results/multi_strategy_2008-06-01_to_2021-05-01_20260417T232746.json

    # Or evaluate the latest result automatically
    python scripts/evaluate_backtest_from_json.py --latest
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPT = (
    PROJECT_ROOT
    / ".claude"
    / "skills"
    / "backtest-expert"
    / "scripts"
    / "evaluate_backtest.py"
)
RESULTS_DIR = PROJECT_ROOT / "backtest_results"
REPORTS_DIR = PROJECT_ROOT / "reports"


def _load_skill_module():
    """Dynamically import the vendored evaluate_backtest module."""
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(
            f"backtest-expert skill script not found at {SKILL_SCRIPT}"
        )
    spec = importlib.util.spec_from_file_location("backtest_expert_eval", SKILL_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {SKILL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Metric derivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyMetrics:
    """Inputs required by the backtest-expert evaluator."""

    strategy_id: str
    display_name: str
    total_trades: int
    win_rate: float  # percent, 0-100
    avg_win_pct: float  # positive percent
    avg_loss_pct: float  # positive percent (absolute)
    max_drawdown_pct: float
    years_tested: float
    num_parameters: int
    slippage_tested: bool


def _pct_return(pnl: float, entry_price: float, shares: float) -> float | None:
    """Return a single trade's percentage return against its entry notional.

    Returns ``None`` when notional is zero so the caller can skip it.
    """
    notional = entry_price * shares
    if notional <= 0:
        return None
    return (pnl / notional) * 100.0


def _safe_mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# Parameter counts per strategy.  Used for the robustness score — the
# backtest-expert evaluator penalises over-parameterisation (>=7 is a red
# flag).  Derived from ``config.yaml -> multi_strategy.strategies.*`` at
# time of integration; update when new knobs are added.
STRATEGY_PARAM_COUNTS: dict[str, int] = {
    "mean_reversion": 5,
    "trend_following": 5,
    "breakout": 5,
    "sentiment_combo": 6,
}


def _estimate_years(from_date: str, to_date: str) -> float:
    """Return the backtest length in years (fractional)."""
    try:
        d1 = datetime.fromisoformat(from_date)
        d2 = datetime.fromisoformat(to_date)
    except ValueError:
        return 0.0
    delta = d2 - d1
    return round(delta.days / 365.25, 2)


def derive_metrics(
    strategy: dict[str, Any],
    from_date: str,
    to_date: str,
) -> StrategyMetrics:
    """Translate a single ``strategies[]`` entry into evaluator inputs."""
    strategy_id = strategy.get("strategy_id", "unknown")
    display_name = strategy.get("display_name", strategy_id)
    trades = strategy.get("trades") or []

    wins_pct: list[float] = []
    losses_pct: list[float] = []
    for t in trades:
        pnl = t.get("pnl_usd")
        entry = t.get("entry_price")
        shares = t.get("shares")
        if pnl is None or entry is None or shares is None:
            continue
        ret = _pct_return(pnl, entry, shares)
        if ret is None:
            continue
        if pnl > 0:
            wins_pct.append(ret)
        elif pnl < 0:
            losses_pct.append(abs(ret))

    total_trades = int(strategy.get("total_trades") or len(trades))
    win_rate = float(strategy.get("win_rate") or 0.0)

    return StrategyMetrics(
        strategy_id=strategy_id,
        display_name=display_name,
        total_trades=total_trades,
        win_rate=win_rate,
        avg_win_pct=round(_safe_mean(wins_pct), 3),
        avg_loss_pct=round(_safe_mean(losses_pct), 3),
        max_drawdown_pct=float(strategy.get("max_drawdown_pct") or 0.0),
        years_tested=_estimate_years(from_date, to_date),
        num_parameters=STRATEGY_PARAM_COUNTS.get(strategy_id, 5),
        # Our backtester models commission + slippage in multi_strategy_backtest.
        slippage_tested=True,
    )


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _latest_backtest_file() -> Path:
    """Return the most recently modified multi_strategy backtest result."""
    candidates = sorted(
        RESULTS_DIR.glob("multi_strategy_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No multi_strategy_*.json found in {RESULTS_DIR}. "
            "Run a backtest first."
        )
    return candidates[0]


def _write_report(
    evaluate_fn,
    to_markdown_fn,
    metrics: StrategyMetrics,
    output_dir: Path,
    timestamp: str,
) -> tuple[Path, Path, dict]:
    """Run the evaluator for a single strategy and write JSON + Markdown."""
    result = evaluate_fn(
        total_trades=metrics.total_trades,
        win_rate=metrics.win_rate,
        avg_win_pct=metrics.avg_win_pct,
        avg_loss_pct=metrics.avg_loss_pct,
        max_drawdown_pct=metrics.max_drawdown_pct,
        years_tested=metrics.years_tested,
        num_parameters=metrics.num_parameters,
        slippage_tested=metrics.slippage_tested,
    )

    # Enrich with identity so downstream skills (strategy-pivot-designer)
    # can key on strategy_id.
    result["strategy_id"] = metrics.strategy_id
    result["display_name"] = metrics.display_name

    stem = f"backtest_eval_{metrics.strategy_id}_{timestamp}"
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"

    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(to_markdown_fn(result), encoding="utf-8")
    return json_path, md_path, result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate each strategy in a multi_strategy backtest result using "
            "the backtest-expert scoring framework."
        )
    )
    parser.add_argument(
        "backtest_json",
        nargs="?",
        help="Path to a backtest_results/*.json (omit with --latest)",
    )
    parser.add_argument(
        "--latest",
        action="store_true",
        help="Evaluate the most recent multi_strategy_*.json",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPORTS_DIR),
        help="Where eval reports are written (default: reports/)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.latest:
        backtest_path = _latest_backtest_file()
    elif args.backtest_json:
        backtest_path = Path(args.backtest_json).resolve()
    else:
        print("Error: provide a backtest JSON path or --latest", file=sys.stderr)
        return 2

    if not backtest_path.exists():
        print(f"Error: file not found: {backtest_path}", file=sys.stderr)
        return 2

    data = json.loads(backtest_path.read_text())
    strategies = data.get("strategies") or []
    if not strategies:
        print(
            f"Error: no 'strategies' array in {backtest_path}", file=sys.stderr
        )
        return 2

    from_date = data.get("from_date") or ""
    to_date = data.get("to_date") or ""

    skill = _load_skill_module()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    print(f"Source backtest : {backtest_path.name}")
    print(f"Period          : {from_date} → {to_date}")
    print(f"Output directory: {output_dir}")
    print()

    rows: list[str] = []
    for s in strategies:
        metrics = derive_metrics(s, from_date, to_date)
        if metrics.total_trades == 0:
            rows.append(
                f"{metrics.strategy_id:<20} SKIP (no trades)"
            )
            continue

        json_path, _, result = _write_report(
            skill.evaluate,
            skill.to_markdown,
            metrics,
            output_dir,
            timestamp,
        )
        rows.append(
            f"{metrics.strategy_id:<20} "
            f"{result['total_score']:>3}/100  "
            f"{result['verdict']:<8}  "
            f"trades={metrics.total_trades:<4}  "
            f"winrate={metrics.win_rate:.1f}%  "
            f"PF={result['profit_factor']:.2f}  "
            f"→ {json_path.name}"
        )

    print("Per-strategy verdicts")
    print("---------------------")
    for row in rows:
        print(row)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

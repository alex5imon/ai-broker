"""Render the daily self-improvement report as Markdown.

The report is the entire artifact the agent produces. The PR adds a single
file under ``trading_bot/docs/self_improve_reports/YYYY-MM-DD.md`` containing:
  - Postmortem table per strategy
  - Each proposal: rule, rationale, evidence
  - A/B backtest result (baseline vs candidate)
  - A "Suggested patch" YAML block the human can apply by hand

The agent never edits config.yaml directly — patches are advisory text.
"""

from __future__ import annotations

from datetime import date
from typing import Iterable

from trading_bot.self_improve.backtest_gate import BacktestComparison, StrategyMetrics
from trading_bot.self_improve.hypotheses import Proposal
from trading_bot.self_improve.postmortem import StrategyStats


def _fmt_pf(pf: float | None) -> str:
    if pf is None:
        return "n/a"
    if pf == float("inf"):
        return "inf"
    return f"{pf:.2f}"


def _render_postmortem_table(stats_by_strategy: dict[str, StrategyStats]) -> str:
    if not stats_by_strategy:
        return "_No strategies evaluated._\n"

    lines = [
        "| Strategy | Trades | Win rate | PF | Net P&L (USD) | Avg win | Avg loss | Avg hold (min) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for sid, s in stats_by_strategy.items():
        lines.append(
            f"| `{sid}` | {s.n_trades} | {s.win_rate:.1%} | {_fmt_pf(s.profit_factor)} | "
            f"{s.total_pnl_usd:+,.2f} | {s.avg_win_usd:+,.2f} | {s.avg_loss_usd:+,.2f} | "
            f"{s.avg_hold_minutes:.0f} |"
        )

    parts = ["\n".join(lines), ""]
    parts.append("**Exit reasons:**\n")
    for sid, s in stats_by_strategy.items():
        if s.n_trades == 0:
            parts.append(f"- `{sid}`: no closed trades in window")
            continue
        breakdown = ", ".join(
            f"{reason}={count}" for reason, count in sorted(s.exit_reason_counts.items())
        )
        parts.append(f"- `{sid}`: {breakdown}")
    parts.append("")
    return "\n".join(parts)


def _render_metrics_row(label: str, m: StrategyMetrics) -> str:
    return (
        f"| {label} | {m.n_trades} | {m.return_pct:+.2f}% | "
        f"{m.max_drawdown_pct:.2f}% | {m.win_rate:.1%} | "
        f"{_fmt_pf(m.profit_factor)} | {m.sharpe_approx:+.2f} |"
    )


def _render_comparison(comp: BacktestComparison) -> str:
    p = comp.proposal
    status = "PASS" if comp.passed else "FAIL"
    param_str = ".".join(p.param_path)
    lines = [
        f"### {p.rule_id} — `{p.strategy_id}.{param_str}`: "
        f"{p.current_value} -> {p.proposed_value}  [{status}]",
        "",
        f"**Rationale.** {p.rationale}",
        "",
        "**Evidence (postmortem window):**",
        "",
    ]
    for k, v in p.evidence.items():
        lines.append(f"- `{k}`: {v}")
    lines.append("")

    lines.append("**Backtest A/B (same window for both runs):**")
    lines.append("")
    lines.append(
        "| Run | Trades | Return | Max DD | Win rate | PF | Sharpe |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    lines.append(_render_metrics_row("Baseline", comp.baseline))
    lines.append(_render_metrics_row("Candidate", comp.candidate))
    lines.append("")
    lines.append(f"**Gate decision:** {comp.reason}")
    lines.append("")

    if comp.passed:
        lines.append("**Suggested patch** (apply by hand if you agree):")
        lines.append("")
        lines.append("```yaml")
        lines.append(f"# trading.strategies.{p.strategy_id}")
        lines.append(f"{param_str}: {_yaml_value(p.proposed_value)}  # was {_yaml_value(p.current_value)}")
        lines.append("```")
        lines.append("")

    return "\n".join(lines)


def _yaml_value(v: float) -> str:
    if float(v).is_integer():
        return str(int(v))
    return f"{v:g}"


def render_markdown(
    *,
    report_date: date,
    window_days: int,
    stats_by_strategy: dict[str, StrategyStats],
    proposals: list[Proposal],
    comparisons: list[BacktestComparison],
    backtest_window: tuple[date, date] | None = None,
    backtest_universe: Iterable[str] | None = None,
) -> str:
    """Render the full report as a single Markdown string."""
    parts: list[str] = [
        f"# Self-improvement review — {report_date.isoformat()}",
        "",
        f"_Postmortem window: last {window_days} days._",
    ]
    if backtest_window is not None:
        bt_from, bt_to = backtest_window
        parts.append(
            f"_Backtest window: {bt_from.isoformat()} to {bt_to.isoformat()}._"
        )
    if backtest_universe is not None:
        parts.append(f"_Backtest universe: {', '.join(backtest_universe)}._")
    parts.append("")

    parts.append("## Postmortem")
    parts.append("")
    parts.append(_render_postmortem_table(stats_by_strategy))

    parts.append("## Proposals")
    parts.append("")
    if not proposals:
        parts.append(
            "_No rule fired. Either the postmortem is within tolerance of "
            "validated baselines, or there isn't enough trade evidence yet "
            "(minimum trades-per-window threshold not met)._"
        )
        parts.append("")
    elif not comparisons:
        parts.append(
            "_Proposals were generated but the backtest gate did not run. "
            "Check the agent log._"
        )
        parts.append("")
        for p in proposals:
            parts.append(f"- {p.rule_id} on `{p.strategy_id}`: {p.rationale}")
        parts.append("")
    else:
        for comp in comparisons:
            parts.append(_render_comparison(comp))

    parts.append("---")
    parts.append("")
    parts.append(
        "_This report is advisory. The agent never edits `config.yaml`. "
        "Apply suggested patches by hand only after independent review._"
    )
    parts.append("")
    return "\n".join(parts)

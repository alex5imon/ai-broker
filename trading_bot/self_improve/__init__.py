"""Self-improvement research agent.

Runs at end of trading day. Produces a markdown report containing:
  1. Postmortem of recent trades (per-strategy stats over rolling window)
  2. Hypotheses for config tweaks (deterministic, rule-based, bounded steps)
  3. Backtest A/B results validating each hypothesis on historical data

The report is committed via a draft PR. Humans review and apply the patch
manually. The agent never edits ``config.yaml`` directly.
"""

from trading_bot.self_improve.postmortem import StrategyStats, compute_window_stats
from trading_bot.self_improve.hypotheses import Proposal, propose
from trading_bot.self_improve.backtest_gate import BacktestComparison, evaluate
from trading_bot.self_improve.report import render_markdown

__all__ = [
    "StrategyStats",
    "compute_window_stats",
    "Proposal",
    "propose",
    "BacktestComparison",
    "evaluate",
    "render_markdown",
]

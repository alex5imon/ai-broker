"""Self-improvement and reconciliation tooling.

The agent (postmortem + hypotheses + backtest gate + report) runs at end
of trading day and produces a draft-PR markdown report. The reconcile
module is a standalone read-only research tool that compares local
SQLite state against the live Alpaca account and produces a markdown
inventory of discrepancies. Neither edits the live DB or submits orders.
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

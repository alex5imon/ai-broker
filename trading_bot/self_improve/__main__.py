"""CLI entrypoint: orchestrates postmortem, hypotheses, backtest gate, report.

Usage:
    python -m trading_bot.self_improve \\
        --window-days 20 \\
        --bt-from 2026-02-01 --bt-to 2026-04-29 \\
        --tickers SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC \\
        --out trading_bot/docs/self_improve_reports
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from trading_bot.config import Config
from trading_bot.log_setup import setup_logging
from trading_bot.self_improve.backtest_gate import (
    evaluate,
    make_multi_intraday_runner,
)
from trading_bot.self_improve.hypotheses import propose
from trading_bot.self_improve.postmortem import summarize_all
from trading_bot.self_improve.report import render_markdown

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Daily self-improvement research agent for the trading bot.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--db", default=None,
        help="Path to SQLite DB (default: config's database.path)",
    )
    parser.add_argument(
        "--window-days", type=int, default=20,
        help="Postmortem window in days (default: 20)",
    )
    parser.add_argument(
        "--bt-from", type=date.fromisoformat, required=True,
        help="Backtest window start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--bt-to", type=date.fromisoformat, required=True,
        help="Backtest window end date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--tickers", default="SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC",
        help="Comma-separated tickers for the multi-ticker intraday backtest",
    )
    parser.add_argument(
        "--cash-per-strategy", type=float, default=2500.0,
        help="USD allocated per strategy in the backtest (default: 2500)",
    )
    parser.add_argument(
        "--out", default="trading_bot/docs/self_improve_reports",
        help="Output directory for the report file "
             "(default: trading_bot/docs/self_improve_reports)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip the backtest gate (postmortem + proposals only)",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    db_path = args.db or config.db_path
    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    strategy_configs = config.get_strategy_configs()
    active_strategy_ids = [
        sid for sid, cfg in strategy_configs.items()
        if cfg.get("enabled", True)
    ]
    if not active_strategy_ids:
        logger.error("No enabled strategies in config; nothing to review")
        return 2

    conn = sqlite3.connect(db_path)
    try:
        stats_by_strategy = summarize_all(
            conn, active_strategy_ids, args.window_days,
            now=datetime.now(timezone.utc),
        )
    finally:
        conn.close()

    for sid, s in stats_by_strategy.items():
        logger.info(
            "Postmortem %s: n=%d wr=%.1f%% pf=%s pnl=%.2f",
            sid, s.n_trades, s.win_rate * 100,
            f"{s.profit_factor:.2f}" if s.profit_factor else "n/a",
            s.total_pnl_usd,
        )

    proposals = propose(stats_by_strategy, strategy_configs)
    logger.info("Generated %d proposals", len(proposals))

    comparisons = []
    if proposals and not args.dry_run:
        tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
        runner = make_multi_intraday_runner(
            tickers, args.bt_from, args.bt_to,
            cash_per_strategy_usd=args.cash_per_strategy,
        )
        comparisons = await evaluate(proposals, config, runner)

    report_md = render_markdown(
        report_date=date.today(),
        window_days=args.window_days,
        stats_by_strategy=stats_by_strategy,
        proposals=proposals,
        comparisons=comparisons,
        backtest_window=(args.bt_from, args.bt_to) if not args.dry_run else None,
        backtest_universe=(
            [t.strip() for t in args.tickers.split(",") if t.strip()]
            if not args.dry_run else None
        ),
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date.today().isoformat()}.md"
    out_path.write_text(report_md, encoding="utf-8")
    logger.info("Report written to %s", out_path)
    print(str(out_path))
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("self_improve")
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())

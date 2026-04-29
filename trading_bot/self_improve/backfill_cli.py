"""CLI wrapper around alpaca_backfill.

Usage:
    python -m trading_bot.self_improve.backfill_cli --dry-run
    python -m trading_bot.self_improve.backfill_cli
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from pathlib import Path

from trading_bot.config import Config
from trading_bot.env import resolve_alpaca_env
from trading_bot.log_setup import setup_logging
from trading_bot.self_improve.alpaca_backfill import backfill

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="One-shot: backfill closed-trade rows from Alpaca order history.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: config's database.path)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Pair candidates and log what would be inserted; do not write to the DB",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    db_path = args.db or config.db_path
    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    api_key, secret_key, is_paper = resolve_alpaca_env()
    if not api_key or not secret_key:
        logger.error(
            "Alpaca credentials not found. Set ALPACA_PAPER_KEY_ID + "
            "ALPACA_PAPER_SECRET (or ALPACA_API_KEY + ALPACA_SECRET_KEY) "
            "in your environment or .env file."
        )
        return 2

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=is_paper)
    logger.info("Connected to Alpaca (%s)", "paper" if is_paper else "live")

    conn = sqlite3.connect(db_path)
    try:
        report = await backfill(conn, client, dry_run=args.dry_run)
    finally:
        conn.close()

    logger.info(
        "Backfill complete: candidates=%d inserted=%d no_exit_found=%d dry_run=%s",
        report.candidates_found, report.inserted,
        report.no_exit_found, report.dry_run,
    )
    if report.candidates_found == 0:
        logger.info("No candidates — either nothing to backfill or all rows already done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("alpaca_backfill")
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())

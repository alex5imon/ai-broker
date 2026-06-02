"""CLI wrapper around ``resolve_reconciliation_mismatch``.

Resolves stale ``reconciliation_mismatch`` trades rows that the nightly
``alpaca_backfill`` can never pair with a real Alpaca fill: phantom
round-trips are voided to $0, genuinely-orphaned exits are flagged for a
human via ntfy. Run *after* the backfill in the nightly daily-review job so
the backfill heals everything it can first.

Usage:
    python -m trading_bot.self_improve.resolve_cli --dry-run
    python -m trading_bot.self_improve.resolve_cli
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
from trading_bot.self_improve.resolve_reconciliation_mismatch import (
    MIN_AGE_DAYS,
    AlertFn,
    resolve,
)

logger = logging.getLogger(__name__)


def _build_alert(config: Config) -> AlertFn | None:
    """Wire a best-effort ntfy alert callback from config. Returns None (logs
    only) if the Notifier can't be constructed — a missing alert channel must
    never block the resolve pass."""
    try:
        from trading_bot.notifications.notifier import Notifier

        notifier = Notifier(config.raw_section())

        def _alert(title: str, message: str) -> None:
            notifier.send_sync(title, message, priority=4, tags=["warning"])

        return _alert
    except Exception:
        logger.warning("Could not construct Notifier — unresolved rows will be "
                       "logged but not pushed", exc_info=True)
        return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve stale reconciliation_mismatch rows the backfill "
                    "can't pair (void phantoms, flag real orphans).",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--db", default=None,
        help="SQLite path (default: config's database.path)",
    )
    parser.add_argument(
        "--min-age-days", type=int, default=MIN_AGE_DAYS,
        help=f"Only resolve rows at least this many days old (default {MIN_AGE_DAYS}). "
             "Younger rows are left for the nightly backfill.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Classify and log what would change; do not write to the DB.",
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

    alert = None if args.dry_run else _build_alert(config)

    conn = sqlite3.connect(db_path)
    try:
        report = await resolve(
            conn, client,
            dry_run=args.dry_run,
            min_age_days=args.min_age_days,
            alert=alert,
        )
    finally:
        conn.close()

    logger.info(
        "Resolve complete: candidates=%d repaired=%d voided=%d unresolved=%d "
        "skipped_too_young=%d dry_run=%s",
        report.candidates, report.repaired, report.voided,
        report.unresolved, report.skipped_too_young, report.dry_run,
    )
    if report.unresolved:
        logger.warning(
            "%d row(s) left UNRESOLVED (entry filled, exit missing) — these "
            "need manual inspection of Alpaca order history.", report.unresolved,
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging("resolve_reconciliation_mismatch")
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())

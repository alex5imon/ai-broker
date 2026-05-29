"""Recompute daily_summaries rows from the trades table.

The live wind-down writer (``main.py::_save_daily_summary``) runs ~16:10 ET,
before the 21:30 UTC daily-review backfill populates the actual exit_price
and pnl_usd on closed trades. As a result every recent daily_summaries
row reads as ``wins=0 losses=0 net_pnl_usd=0`` even on days with dozens
of closed trades.

This script reads the trades table, recomputes the metrics, and uses
``repo.save_daily_summary`` (INSERT OR REPLACE) to overwrite the row.
Run after the backfill so it sees the populated trade rows.

Usage:
    python -m trading_bot.self_improve.recompute_daily_summaries --days 7
    python -m trading_bot.self_improve.recompute_daily_summaries --date 2026-05-05
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from trading_bot.config import Config
from trading_bot.constants import TZ_EASTERN
from trading_bot.db import repository as repo
from trading_bot.log_setup import setup_logging
from trading_bot.reporting.performance import PerformanceCalculator

logger = logging.getLogger(__name__)


def _dates_with_trades(
    conn: sqlite3.Connection, days_back: int,
) -> list[str]:
    """Return YYYY-MM-DD strings for the last ``days_back`` calendar days
    that have at least one trades row with a non-null exit_time.

    Uses ``substr(exit_time, 1, 10)`` rather than SQLite's built-in
    ``date(...)`` because exit_time is stored as an ET-aware ISO string;
    ``date()`` would silently convert to UTC and shift late-ET trades to
    the wrong calendar day. See ``performance.py`` module docstring.
    """
    cutoff: date = (
        datetime.now(tz=TZ_EASTERN).date() - timedelta(days=days_back)
    )
    rows = conn.execute(
        "SELECT DISTINCT substr(exit_time, 1, 10) AS d FROM trades "
        "WHERE exit_time IS NOT NULL AND substr(exit_time, 1, 10) >= ? "
        "ORDER BY d",
        (cutoff.isoformat(),),
    ).fetchall()
    return [str(r[0]) for r in rows if r[0]]


def _resolve_db_path(conn: sqlite3.Connection) -> str:
    """Return the file path backing ``conn`` so PerformanceCalculator can
    open its own read connection against the same DB. Empty for
    ``:memory:`` connections, which would make the metrics path read an
    unrelated empty file — the caller must pass ``db_path`` explicitly
    in that case.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    # PRAGMA database_list returns (seq, name, file). The 'main' entry's
    # file is empty for in-memory or temp DBs.
    return str(row[2]) if row and row[2] else ""


def _equity_for_date(
    conn: sqlite3.Connection, target_date: str,
) -> float | None:
    """Use the existing daily_summaries row's account_equity_usd if any,
    so the recompute doesn't reset the equity column to a stale value.
    daily_summaries.account_equity_usd is NOT NULL — without a value we
    skip the recompute rather than write a 0.0 lie.
    """
    row = conn.execute(
        "SELECT account_equity_usd FROM daily_summaries WHERE date = ?",
        (target_date,),
    ).fetchone()
    if row is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def recompute_for_dates(
    conn: sqlite3.Connection,
    dates: Iterable[str],
    *,
    phase_resolver: Callable[[float], int],
    dry_run: bool,
    db_path: str | None = None,
) -> int:
    """Recompute ``daily_summaries`` rows for ``dates``. Returns the
    number of rows written (or that would be written, in dry-run).

    ``phase_resolver`` maps a date's ``account_equity_usd`` to its
    operating phase. It MUST be equity-driven: this script runs in the
    daily-review process, which never calls
    ``main.py::_refresh_phase_from_equity``, so a bare
    ``Config.get_phase()`` would return the load-time default
    (``Phase.MICRO`` = 1) and stamp every recomputed row phase=1 —
    overwriting the correct phase the live tick wrote. Resolving per-date
    from the row's own equity keeps the ``phase`` column self-consistent
    with the equity being written. See ``main`` for the canonical wiring.

    ``db_path`` is required when ``conn`` is in-memory or otherwise opaque
    to ``PerformanceCalculator`` (which opens its own read connection).
    Pass the actual file path so the metrics calculator can read the
    same trades the connection sees. For test connections backed by a
    temp file, the path can be derived via ``conn.execute('PRAGMA
    database_list')`` — see the CLI ``main`` for the canonical wiring.
    """
    perf = PerformanceCalculator(db_path or _resolve_db_path(conn))
    written = 0
    for d in dates:
        equity = _equity_for_date(conn, d)
        if equity is None:
            logger.warning(
                "Skipping %s: no existing daily_summaries row to read "
                "account_equity from. Run the live bot's wind-down once "
                "to seed it, then re-run this script.", d,
            )
            continue
        try:
            metrics = perf.calculate_daily_metrics(d)
        except Exception:
            logger.exception("Daily metrics calculation failed for %s", d)
            continue

        summary = {
            "date": d,
            "total_trades": metrics.get("total_trades", 0),
            "wins": metrics.get("wins", 0),
            "losses": metrics.get("losses", 0),
            "gross_pnl_usd": metrics.get("gross_pnl_usd", 0.0),
            "commissions_usd": metrics.get("commissions_usd", 0.0),
            "net_pnl_usd": metrics.get("net_pnl_usd", 0.0),
            "account_equity_usd": equity,
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "win_rate": metrics.get("win_rate"),
            "avg_win_usd": metrics.get("avg_win"),
            "avg_loss_usd": metrics.get("avg_loss"),
            "profit_factor": metrics.get("profit_factor"),
            "phase": phase_resolver(equity),
            "us_trades": metrics.get("us_trades", 0),
            "notes": "recomputed:post_backfill",
        }
        logger.info(
            "%sRecompute %s: trades=%d wins=%d losses=%d net=$%.2f",
            "[DRY-RUN] " if dry_run else "",
            d, summary["total_trades"], summary["wins"],
            summary["losses"], summary["net_pnl_usd"],
        )
        if dry_run:
            written += 1
            continue
        repo.save_daily_summary(conn, summary)
        written += 1

    if not dry_run:
        conn.commit()
    return written


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute daily_summaries rows from the trades table.",
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", default=None,
                        help="SQLite path (default: config's database.path)")
    parser.add_argument(
        "--days", type=int, default=7,
        help="Recompute summaries for the last N days that have closed "
             "trades (default 7).",
    )
    parser.add_argument(
        "--date", default=None,
        help="Recompute a single YYYY-MM-DD date (overrides --days).",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging("recompute_daily_summaries")
    args = _parse_args(argv)
    config = Config.load(args.config)
    db_path = args.db or config.db_path
    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        if args.date:
            dates = [args.date]
        else:
            dates = _dates_with_trades(conn, args.days)
            if not dates:
                logger.info("No closed-trade dates in the last %d days.",
                            args.days)
                return 0
        # Resolve phase per-date from each row's equity via the pure
        # ``resolve_phase`` (no cache mutation). A bare ``get_phase()``
        # here would return the load-time default (MICRO=1) because this
        # process never anchors phase to live equity the way the main
        # tick does; ``get_phase(equity_usd=...)`` would work but leaks
        # the last date's phase into config._phase. See
        # recompute_for_dates.
        written = recompute_for_dates(
            conn, dates,
            phase_resolver=lambda equity: config.resolve_phase(equity).value,
            dry_run=args.dry_run,
            db_path=db_path,
        )
    finally:
        conn.close()

    logger.info("Recomputed %d daily_summary row(s) (dry_run=%s)",
                written, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

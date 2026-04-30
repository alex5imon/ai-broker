"""CLI entry point: ``python -m trading_bot.self_improve.reconcile``.

Read-only by construction — the only side effects are logging and writing
the markdown report file. Pulls Alpaca state via the unified env resolver
so paper/live selection follows the same contract as the live bot.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trading_bot.self_improve.reconcile.alpaca_fetch import (
    AlpacaFetcher,
    AlpacaState,
    fetch_alpaca_state,
)
from trading_bot.self_improve.reconcile.db_loaders import (
    load_db_positions,
    load_db_trades,
    load_strategy_enabled_map,
)
from trading_bot.self_improve.reconcile.report import (
    ReconcileReport,
    build_report,
    render_markdown,
)

logger: logging.Logger = logging.getLogger(__name__)

DEFAULT_LOOKBACK_DAYS: int = 30


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m trading_bot.self_improve.reconcile",
        description=(
            "Read-only reconciliation between the local SQLite DB and the "
            "live Alpaca account. Writes a markdown report; touches nothing."
        ),
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to the SQLite DB. Defaults to config.yaml's database.path "
            "(typically trading_bot/data/trading_bot.db)."
        ),
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (used for db path + strategy enabled map).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "Lower bound for Alpaca order pull as YYYY-MM-DD. "
            f"Default: {DEFAULT_LOOKBACK_DAYS} days back."
        ),
    )
    parser.add_argument(
        "--until",
        default=None,
        help="Upper bound for Alpaca order pull as YYYY-MM-DD. Default: now.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Output path for the markdown report. Default: "
            "trading_bot/docs/self_improve_reports/reconcile_<UTC date>.md."
        ),
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args(argv)


def _resolve_db_path(args: argparse.Namespace) -> str:
    if args.db:
        return str(args.db)
    import yaml

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")
    with config_path.open("r", encoding="utf-8") as fh:
        cfg: Any = yaml.safe_load(fh) or {}
    db_block: Any = cfg.get("database") or {}
    return str(db_block.get("path") or "trading_bot/data/trading_bot.db")


def _resolve_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    until: datetime
    if args.until:
        until = datetime.fromisoformat(args.until).replace(tzinfo=timezone.utc)
    else:
        until = datetime.now(tz=timezone.utc)
    since: datetime
    if args.since:
        since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    else:
        since = until - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return since, until


def _build_alpaca_client() -> AlpacaFetcher:
    """Construct a TradingClient using the unified env resolver."""
    from alpaca.trading.client import TradingClient

    from trading_bot.env import resolve_alpaca_env

    api_key, secret_key, is_paper = resolve_alpaca_env()
    if not api_key or not secret_key:
        raise RuntimeError(
            "Missing Alpaca credentials. Set ALPACA_PAPER_KEY_ID + "
            "ALPACA_PAPER_SECRET (or ALPACA_LIVE_*) in the environment."
        )
    return TradingClient(api_key=api_key, secret_key=secret_key, paper=is_paper)


def _resolve_output_path(args: argparse.Namespace) -> Path:
    if args.output:
        return Path(args.output).expanduser().resolve()
    today: str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    return Path("trading_bot/docs/self_improve_reports").resolve() / f"reconcile_{today}.md"


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path: str = _resolve_db_path(args)
    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2
    since, until = _resolve_window(args)
    strategy_enabled: dict[str, bool] = load_strategy_enabled_map(args.config)
    logger.info(
        "Reconciling DB=%s window=[%s, %s] strategies=%s",
        db_path,
        since.date(),
        until.date(),
        strategy_enabled,
    )

    client: AlpacaFetcher = _build_alpaca_client()
    state: AlpacaState = fetch_alpaca_state(client, since=since, until=until)
    logger.info(
        "Alpaca state: %d positions, %d orders in window",
        len(state.positions_by_symbol),
        len(state.orders_by_id),
    )

    conn: sqlite3.Connection = sqlite3.connect(db_path)
    try:
        db_positions: list[dict[str, Any]] = load_db_positions(conn)
        db_trades: list[dict[str, Any]] = load_db_trades(conn)
    finally:
        conn.close()
    logger.info(
        "DB rows: %d positions, %d trades",
        len(db_positions),
        len(db_trades),
    )

    report: ReconcileReport = build_report(
        db_path=db_path,
        state=state,
        db_positions=db_positions,
        db_trades=db_trades,
        strategy_enabled=strategy_enabled,
        since=since,
        until=until,
    )
    markdown: str = render_markdown(report)

    output_path: Path = _resolve_output_path(args)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")
    logger.info("Wrote reconciliation report to %s", output_path)
    print(str(output_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

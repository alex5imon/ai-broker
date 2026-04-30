"""One-shot operational tool: flatten orphan positions on Alpaca.

Surfaced by the Phase 1 reconcile report (see
docs/self_improve_followups.md task #3). Three open positions on the
paper account no live tick code is managing:

  - SPY +1   strategy=breakout            (DISABLED in config)
  - XLRE +20 strategy=trend_following     (DISABLED in config)
  - QQQ -1   strategy=unknown             (SHORT — not a strategy archetype)

For each orphan this script:
  1. Re-checks the position is still live on Alpaca (idempotent).
  2. Cancels any open child orders (stop / target / trail).
  3. Submits a MARKET order for the OPPOSITE side & matching qty
     (SELL for long, BUY for short).
  4. Updates positions.status = 'CLOSED' in the local DB.

SAFETY:
  - --dry-run is the default. The script prints what it would do and
    exits without touching Alpaca or the DB.
  - --execute is required to actually submit orders.
  - --tickers can scope the run to a subset (default: SPY,XLRE,QQQ).
  - Each ticker runs independently; one failure does not block the rest.
  - Submitted orders are always MARKET DAY — fills near current price,
    expires at session close if untouched.

Run:
    python -m trading_bot.self_improve.flatten_orphans               # dry-run
    python -m trading_bot.self_improve.flatten_orphans --execute     # send orders
    python -m trading_bot.self_improve.flatten_orphans --tickers QQQ # one only
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

from trading_bot.config import Config
from trading_bot.env import resolve_alpaca_env
from trading_bot.log_setup import setup_logging

logger = logging.getLogger(__name__)

ET = ZoneInfo("US/Eastern")

# Default scope. Edit only if reconcile shows different orphans.
DEFAULT_ORPHAN_TICKERS: tuple[str, ...] = ("SPY", "XLRE", "QQQ")


@dataclass(frozen=True)
class OrphanPlan:
    """What we'd do to flatten a single orphan position."""

    ticker: str
    alpaca_qty: float          # signed: negative = short
    flatten_side: str          # "BUY" or "SELL"
    flatten_qty: float         # absolute value
    db_position_id: int | None
    child_order_ids_to_cancel: list[str]


def _build_plan(
    alpaca_position,
    db_position_id: int | None,
    child_order_ids: list[str],
) -> OrphanPlan:
    qty = float(alpaca_position.qty)
    abs_qty = abs(qty)
    side = "BUY" if qty < 0 else "SELL"
    return OrphanPlan(
        ticker=str(alpaca_position.symbol),
        alpaca_qty=qty,
        flatten_side=side,
        flatten_qty=abs_qty,
        db_position_id=db_position_id,
        child_order_ids_to_cancel=child_order_ids,
    )


def _find_db_position(conn: sqlite3.Connection, ticker: str) -> tuple[int | None, list[str]]:
    """Return (position_id, child_order_ids) for the live DB row for ``ticker``.

    "Live" = not yet CLOSED / ENTRY_FAILED. Returns (None, []) if no row.
    """
    row = conn.execute(
        """
        SELECT id, alpaca_stop_order_id, alpaca_target_order_id, alpaca_trail_order_id
          FROM positions
         WHERE ticker = ?
           AND status NOT IN ('CLOSED', 'ENTRY_FAILED')
         ORDER BY entry_time DESC
         LIMIT 1
        """,
        (ticker,),
    ).fetchone()
    if row is None:
        return None, []
    pos_id, stop_id, target_id, trail_id = row
    child_ids = [oid for oid in (stop_id, target_id, trail_id) if oid]
    return int(pos_id), child_ids


def _execute_one(
    plan: OrphanPlan,
    client,
    conn: sqlite3.Connection,
) -> bool:
    """Cancel children, submit flatten market order, update DB.

    Returns True on full success. On failure, logs and returns False so
    the next orphan can still be attempted.
    """
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

    # 1. Cancel child orders. Best-effort; an already-canceled child is fine.
    for child_id in plan.child_order_ids_to_cancel:
        try:
            client.cancel_order_by_id(child_id)
            logger.info("Cancelled child order %s for %s", child_id, plan.ticker)
        except Exception as exc:
            logger.warning(
                "Could not cancel child order %s for %s (may already be done): %s",
                child_id, plan.ticker, exc,
            )

    # 2. Submit the flatten order. Refuse if Alpaca side enum doesn't match.
    side_enum = OrderSide.BUY if plan.flatten_side == "BUY" else OrderSide.SELL
    request = MarketOrderRequest(
        symbol=plan.ticker,
        qty=plan.flatten_qty,
        side=side_enum,
        type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
    )
    try:
        order = client.submit_order(order_data=request)
    except Exception:
        logger.exception("FAILED to submit flatten order for %s", plan.ticker)
        return False

    logger.info(
        "Submitted FLATTEN %s %s %.6f (alpaca_id=%s)",
        plan.flatten_side, plan.ticker, plan.flatten_qty, order.id,
    )

    # 3. Update DB only after the order is accepted by Alpaca.
    if plan.db_position_id is not None:
        try:
            now_str = datetime.now(tz=ET).isoformat()
            conn.execute(
                "UPDATE positions SET status = 'CLOSED', updated_at = ? "
                "WHERE id = ? AND status NOT IN ('CLOSED', 'ENTRY_FAILED')",
                (now_str, plan.db_position_id),
            )
            conn.commit()
            logger.info("Updated DB positions.id=%d -> CLOSED", plan.db_position_id)
        except Exception:
            logger.exception(
                "Order accepted but DB update failed for positions.id=%d. "
                "Reconcile after the fill.", plan.db_position_id,
            )
            return False

    return True


def plan_and_execute(
    *,
    db_path: str,
    tickers: Iterable[str],
    execute: bool,
) -> int:
    """Return shell exit code. 0 = all planned/executed cleanly."""
    target_tickers = {t.upper() for t in tickers}

    api_key, secret_key, is_paper = resolve_alpaca_env()
    if not api_key or not secret_key:
        logger.error(
            "Alpaca credentials not found. Set ALPACA_PAPER_KEY_ID + "
            "ALPACA_PAPER_SECRET (or ALPACA_API_KEY + ALPACA_SECRET_KEY)."
        )
        return 2

    from alpaca.trading.client import TradingClient
    client = TradingClient(api_key, secret_key, paper=is_paper)
    logger.info("Connected to Alpaca (%s, paper=%s)",
                "live" if not is_paper else "paper", is_paper)

    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    # Pull live Alpaca positions and build per-ticker plans.
    alpaca_positions = client.get_all_positions()
    by_ticker = {str(p.symbol): p for p in alpaca_positions if float(p.qty) != 0}
    logger.info("Alpaca holds %d non-zero positions: %s",
                len(by_ticker), sorted(by_ticker.keys()))

    plans: list[OrphanPlan] = []
    conn = sqlite3.connect(db_path)
    try:
        for ticker in target_tickers:
            if ticker not in by_ticker:
                logger.info("%s: not held on Alpaca — nothing to flatten", ticker)
                continue
            pos_id, child_ids = _find_db_position(conn, ticker)
            plans.append(_build_plan(by_ticker[ticker], pos_id, child_ids))
    finally:
        if not execute:
            conn.close()

    if not plans:
        logger.info("No orphans to flatten. Done.")
        return 0

    # Print the plan unconditionally so dry-run is the same as the first
    # half of an execute run.
    print()
    print("=== Flatten plan ===")
    for p in plans:
        print(
            f"  {p.ticker:6s} alpaca_qty={p.alpaca_qty:+.4f}  "
            f"action={p.flatten_side} {p.flatten_qty} @ MARKET  "
            f"db_id={p.db_position_id}  cancel_children={len(p.child_order_ids_to_cancel)}"
        )
    print()

    if not execute:
        print("DRY RUN — re-run with --execute to send these orders.")
        return 0

    print("EXECUTING — submitting market orders to Alpaca...")
    print()

    failures = 0
    try:
        conn.row_factory = sqlite3.Row
        for plan in plans:
            ok = _execute_one(plan, client, conn)
            if not ok:
                failures += 1
    finally:
        conn.close()

    if failures:
        logger.error("%d/%d flatten action(s) failed — check log", failures, len(plans))
        return 1
    logger.info("All %d orphan(s) flattened successfully", len(plans))
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else "",
    )
    p.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    p.add_argument("--db", default=None,
                   help="SQLite path (default: config's database.path)")
    p.add_argument("--tickers", default=",".join(DEFAULT_ORPHAN_TICKERS),
                   help="Comma-separated tickers to consider (default: SPY,XLRE,QQQ)")
    p.add_argument("--execute", action="store_true",
                   help="Actually submit orders. Without this flag the script is read-only.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging("flatten_orphans")
    args = _parse_args(argv)
    config = Config.load(args.config)
    db_path = args.db or config.db_path
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    return plan_and_execute(db_path=db_path, tickers=tickers, execute=args.execute)


if __name__ == "__main__":
    sys.exit(main())

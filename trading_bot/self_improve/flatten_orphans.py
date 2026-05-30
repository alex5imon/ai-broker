"""One-shot operational tool: flatten orphan positions on Alpaca.

Surfaced by the Phase 1 reconcile report (see
trading_bot/docs/self_improve_followups.md task #3). Three open positions on the
paper account no live tick code is managing:

  - SPY +1   strategy=breakout            (DISABLED in config)
  - XLRE +20 strategy=trend_following     (DISABLED in config)
  - QQQ -1   strategy=unknown             (SHORT — not a strategy archetype)

For each orphan this script:
  1. Re-checks the position is still live on Alpaca (idempotent).
  2. Cancels every open Alpaca order on the ticker (releases held qty).
  3. Submits a MARKET order for the OPPOSITE side & matching qty
     (SELL for long, BUY for short).
  4. Updates positions.status = 'CLOSED' in the local DB.

The market-state-aware order pick is what makes the script robust outside
RTH. Outside market hours, Alpaca queues stop-cancellations until the
next open and the held qty stays reserved, so a normal MARKET DAY
submitted pre-market would be rejected ("insufficient qty available for
order"). Instead we submit MARKET-on-Open (TimeInForce.OPG) when the
clock is closed, which queues for the opening auction and bypasses the
held-qty check on the live order book.

SAFETY:
  - --dry-run is the default. The script prints what it would do and
    exits without touching Alpaca or the DB.
  - --execute is required to actually submit orders.
  - Risk-circuit gate: with --execute, the script reads risk_circuit_state
    first and REFUSES if any kill switch is active (is_paused,
    daily_loss_limit_hit, drawdown_breaker_active, commission_stop_active).
    This guards against an operator flattening a stale ticker list right
    after the bot tripped its loss limit. Override with --force-during-halt
    (logs a WARNING). The halt read is offline, before any broker call.
  - --tickers can scope the run to a subset (default: SPY,XLRE,QQQ).
  - Each ticker runs independently; one failure does not block the rest.
  - Order TIF is chosen from Alpaca's clock:
      * Market open  -> MARKET DAY (fills near current price)
      * Market closed -> MARKET OPG (executes at the next opening cross)

Run:
    python -m trading_bot.self_improve.flatten_orphans               # dry-run
    python -m trading_bot.self_improve.flatten_orphans --execute     # send orders
    python -m trading_bot.self_improve.flatten_orphans --tickers QQQ # one only
    python -m trading_bot.self_improve.flatten_orphans --execute \
        --force-during-halt    # flatten even while a kill switch is active
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
from trading_bot.db import repository as repo
from trading_bot.env import resolve_alpaca_env
from trading_bot.log_setup import setup_logging

logger = logging.getLogger(__name__)

ET = ZoneInfo("US/Eastern")

# Default scope. Edit only if reconcile shows different orphans.
DEFAULT_ORPHAN_TICKERS: tuple[str, ...] = ("SPY", "XLRE", "QQQ")

# Global risk-circuit key. Canonical definition is
# trading_bot.execution.risk_manager._RISK_STATE_KEY; kept as a literal here so
# this operational tool doesn't import the RiskManager. test_flatten_orphans
# asserts the two stay in sync.
_RISK_STATE_KEY: str = "risk_manager:global"

# Halt flags in the persisted risk_circuit_state blob. If any is truthy the
# bot has tripped a kill switch and a manual --execute flatten must not run
# unless explicitly forced.
_HALT_FLAGS: tuple[str, ...] = (
    "is_paused",
    "daily_loss_limit_hit",
    "drawdown_breaker_active",
    "commission_stop_active",
)


def _active_halts(db_path: str) -> list[str]:
    """Return the names of any active risk-circuit halts, or ``[]`` if clear.

    Reads ``risk_circuit_state`` directly from SQLite — no broker connection —
    so the refusal path never opens an Alpaca session. A read failure is
    treated as halted (fail-safe): better to block a flatten than to fire one
    blind to the kill switch.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            state_row = repo.load_risk_state(conn, _RISK_STATE_KEY)
    except Exception:
        logger.exception(
            "Could not read risk_circuit_state from %s; treating as halted",
            db_path,
        )
        return ["risk_state_unreadable"]
    if state_row is None:
        return []
    blob: dict = state_row.get("state") or {}
    return [flag for flag in _HALT_FLAGS if blob.get(flag)]


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


def _choose_tif(is_market_open: bool):
    """Pick the flatten order's TimeInForce based on Alpaca's clock.

    Pulled out as a free function so tests can pin both branches without
    touching the network.

    Market open  -> DAY (immediate fill on the live order book).
    Market closed -> OPG (queued for the next opening cross). OPG bypasses
    the held_for_orders check that blocks a DAY order placed pre-market
    when stop-cancellations are stuck in PENDING_CANCEL until the open.
    """
    from alpaca.trading.enums import TimeInForce
    return TimeInForce.DAY if is_market_open else TimeInForce.OPG


def _execute_one(
    plan: OrphanPlan,
    client,
    conn: sqlite3.Connection,
    *,
    is_market_open: bool,
) -> bool:
    """Cancel children, submit flatten market order, update DB.

    Returns True on full success. On failure, logs and returns False so
    the next orphan can still be attempted.
    """
    from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
    from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus

    # 1. Cancel ALL open orders for this ticker on Alpaca, not just the ones
    # the local DB knows about. Lingering bracket children reserve qty
    # (held_for_orders) and Alpaca will reject a DAY flatten with
    # "insufficient qty available for order" until those are released.
    # When market is closed the cancellations queue (PENDING_CANCEL) and
    # only process at the next open — that's why we use OPG below.
    open_req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[plan.ticker])
    try:
        open_orders = client.get_orders(filter=open_req)
    except Exception:
        logger.exception("Could not list open orders for %s", plan.ticker)
        open_orders = []
    for o in open_orders:
        try:
            client.cancel_order_by_id(str(o.id))
            logger.info(
                "Cancelled open order %s for %s (side=%s qty=%s type=%s)",
                o.id, plan.ticker, o.side, o.qty, o.order_type,
            )
        except Exception as exc:
            logger.warning(
                "Could not cancel order %s for %s (may already be done): %s",
                o.id, plan.ticker, exc,
            )
    # Only poll for cancellation completion when market is open. Outside
    # RTH the cancels stay PENDING_CANCEL until the open and polling
    # would just burn the timeout. Both the cancel and the OPG flatten
    # process together at the auction.
    if open_orders and is_market_open:
        import time
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                still_open = client.get_orders(filter=open_req)
            except Exception:
                still_open = []
            if not still_open:
                logger.info(
                    "All open orders cleared for %s after cancellation",
                    plan.ticker,
                )
                break
            time.sleep(1.0)
        else:
            logger.warning(
                "Open orders for %s did not clear within 30s; "
                "flatten may still fail with insufficient qty",
                plan.ticker,
            )

    # 2. Submit the flatten order. TIF chosen from clock state.
    side_enum = OrderSide.BUY if plan.flatten_side == "BUY" else OrderSide.SELL
    tif = _choose_tif(is_market_open)
    request = MarketOrderRequest(
        symbol=plan.ticker,
        qty=plan.flatten_qty,
        side=side_enum,
        type=OrderType.MARKET,
        time_in_force=tif,
    )
    try:
        order = client.submit_order(order_data=request)
    except Exception:
        logger.exception("FAILED to submit flatten order for %s", plan.ticker)
        return False

    logger.info(
        "Submitted FLATTEN %s %s %.6f (tif=%s, alpaca_id=%s)",
        plan.flatten_side, plan.ticker, plan.flatten_qty, tif.value, order.id,
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
    force_during_halt: bool = False,
) -> int:
    """Return shell exit code. 0 = all planned/executed cleanly."""
    target_tickers = {t.upper() for t in tickers}

    if not Path(db_path).exists():
        logger.error("DB not found at %s", db_path)
        return 2

    # Risk-circuit gate — only when actually placing orders. The read is
    # offline (SQLite) and happens BEFORE any broker call, so a refusal never
    # opens an Alpaca session. dry-run is intentionally unaffected: it only
    # lists positions and prints a plan.
    if execute:
        halts = _active_halts(db_path)
        if halts and not force_during_halt:
            logger.error(
                "Risk circuit is HALTED (%s). Refusing --execute to protect the "
                "account during an active kill switch. Re-run with "
                "--force-during-halt to override (logs a WARNING).",
                ", ".join(halts),
            )
            return 2
        if halts and force_during_halt:
            logger.warning(
                "Risk circuit is HALTED (%s) but --force-during-halt was "
                "supplied — proceeding with flatten anyway.",
                ", ".join(halts),
            )

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

    # Pull live Alpaca positions and the clock state up front. The clock
    # informs the TIF chosen for the flatten orders (see _choose_tif).
    alpaca_positions = client.get_all_positions()
    by_ticker = {str(p.symbol): p for p in alpaca_positions if float(p.qty) != 0}
    logger.info("Alpaca holds %d non-zero positions: %s",
                len(by_ticker), sorted(by_ticker.keys()))

    try:
        clock = client.get_clock()
        is_market_open = bool(getattr(clock, "is_open", False))
    except Exception:
        logger.exception("Could not fetch Alpaca clock; assuming market closed")
        is_market_open = False
    logger.info("Market state: %s", "OPEN" if is_market_open else "CLOSED")

    # Single try/finally wraps the whole DB lifetime — review CRITICAL
    # (conn leak): the prior structure left ``conn`` open for the
    # plan-printing block between the planning and execution try blocks.
    # Any exception in print formatting (e.g., a malformed plan repr)
    # would leak the connection on the execute path.
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        plans: list[OrphanPlan] = []
        for ticker in target_tickers:
            if ticker not in by_ticker:
                logger.info("%s: not held on Alpaca — nothing to flatten", ticker)
                continue
            pos_id, child_ids = _find_db_position(conn, ticker)
            plans.append(_build_plan(by_ticker[ticker], pos_id, child_ids))

        if not plans:
            logger.info("No orphans to flatten. Done.")
            return 0

        # Print the plan unconditionally so dry-run is the same as the
        # first half of an execute run.
        print()
        tif_label = (
            "DAY (market open)" if is_market_open
            else "OPG (market closed — queues for next open)"
        )
        print(f"=== Flatten plan ===  TIF={tif_label}")
        for p in plans:
            print(
                f"  {p.ticker:6s} alpaca_qty={p.alpaca_qty:+.4f}  "
                f"action={p.flatten_side} {p.flatten_qty} @ MARKET  "
                f"db_id={p.db_position_id}  "
                f"cancel_children={len(p.child_order_ids_to_cancel)}"
            )
        print()

        if not execute:
            print("DRY RUN — re-run with --execute to send these orders.")
            return 0

        print("EXECUTING — submitting flatten orders to Alpaca...")
        print()

        # If the planned orphans are exactly the live position set, use
        # the broker-atomic close_all_positions(cancel_orders=True).
        # Broker-side, so it works pre-market when manual cancel +
        # submit is blocked by held_for_orders on PENDING_CANCEL stops.
        planned_tickers = {p.ticker for p in plans}
        live_tickers = set(by_ticker.keys())
        use_bulk = planned_tickers == live_tickers

        if use_bulk:
            logger.info(
                "Planned orphans (%s) == live Alpaca positions; using "
                "close_all_positions(cancel_orders=True) for atomic flatten",
                sorted(planned_tickers),
            )
            failures = _execute_bulk(plans, client, conn)
        else:
            extra = live_tickers - planned_tickers
            logger.warning(
                "Live Alpaca positions (%s) include non-orphan tickers (%s); "
                "falling back to per-ticker close. Some flattens may fail "
                "pre-market due to held_for_orders on pending stop cancels.",
                sorted(live_tickers), sorted(extra),
            )
            failures = 0
            for plan in plans:
                ok = _execute_one(
                    plan, client, conn, is_market_open=is_market_open,
                )
                if not ok:
                    failures += 1

        if failures:
            logger.error(
                "%d/%d flatten action(s) failed — check log",
                failures, len(plans),
            )
            return 1
        logger.info("All %d orphan(s) flattened successfully", len(plans))
        return 0
    finally:
        conn.close()


def _execute_bulk(
    plans: list[OrphanPlan],
    client,
    conn: sqlite3.Connection,
) -> int:
    """Atomic bulk flatten via close_all_positions. Returns failure count."""
    try:
        responses = client.close_all_positions(cancel_orders=True)
    except Exception:
        logger.exception("close_all_positions raised; nothing flattened")
        return len(plans)

    # Each response carries .symbol and .status (HTTP code from the broker).
    # 200 / 207 = accepted; anything else means that ticker did not flatten.
    by_symbol_status: dict[str, int] = {}
    for r in responses:
        sym = str(getattr(r, "symbol", "?"))
        status = int(getattr(r, "status", 0))
        by_symbol_status[sym] = status
        body = getattr(r, "body", None)
        order_id = getattr(body, "id", None) if body is not None else None
        logger.info("Bulk close: %s -> status=%d order_id=%s", sym, status, order_id)

    failures = 0
    now_str = datetime.now(tz=ET).isoformat()
    for plan in plans:
        status = by_symbol_status.get(plan.ticker)
        if status is None or status >= 300:
            logger.error("Bulk close did not flatten %s (status=%s)", plan.ticker, status)
            failures += 1
            continue
        # HTTP 202 = Accepted/queued (e.g. pre-market: queued for the
        # next open). The position is NOT yet flat. Marking the DB row
        # CLOSED here would corrupt position records — review CRITICAL.
        # Leave the DB row alone; the daily reconcile picks it up
        # after the auction fill. Don't count as a failure: the bulk
        # call itself succeeded.
        if status == 202:
            logger.warning(
                "Bulk close %s: HTTP 202 — order queued for next open. "
                "Leaving DB row OPEN; reconcile will close it after fill.",
                plan.ticker,
            )
            continue
        if status != 200:
            # Anything other than 200/202 is a real per-symbol failure.
            logger.error(
                "Bulk close %s: unexpected status %d — treating as failure",
                plan.ticker, status,
            )
            failures += 1
            continue
        # status == 200: synchronously accepted/processed. Safe to mark
        # CLOSED in the DB.
        if plan.db_position_id is not None:
            try:
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
                failures += 1
    return failures


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
    p.add_argument("--force-during-halt", action="store_true",
                   help="Override the risk-circuit halt gate and flatten even when "
                        "a kill switch is active. Requires --execute.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    setup_logging("flatten_orphans")
    args = _parse_args(argv)

    # --force-during-halt only has meaning alongside --execute. On its own it's
    # an operator mistake: warn and exit without touching the broker or DB.
    if args.force_during_halt and not args.execute:
        logger.warning(
            "--force-during-halt has no effect without --execute (the halt gate "
            "only applies when placing orders). Exiting without action."
        )
        return 0

    config = Config.load(args.config)
    db_path = args.db or config.db_path
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    return plan_and_execute(
        db_path=db_path,
        tickers=tickers,
        execute=args.execute,
        force_during_halt=args.force_during_halt,
    )


if __name__ == "__main__":
    sys.exit(main())

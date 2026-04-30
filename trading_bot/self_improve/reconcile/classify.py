"""Per-row classification of ``positions`` and ``trades`` against Alpaca state.

Classifications are deliberately fine-grained so the report doubles as the
inventory needed to plan Phase 2 (DB migration) and Phase 3 (live order
logic fixes). See ``trading_bot/docs/self_improve_followups.md`` for the data-layer bug
hypotheses each label is designed to surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Mapping

from trading_bot.self_improve.reconcile.alpaca_fetch import (
    AlpacaOrderRec,
    AlpacaState,
    parse_iso,
    to_float,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

QTY_TOLERANCE: float = 1e-4
ENTRY_FILL_LOOKBACK_HOURS: int = 6
EXIT_FILL_LOOKBACK_DAYS: int = 14


# ---------------------------------------------------------------------------
# Classifications
# ---------------------------------------------------------------------------


class PositionClass(str, Enum):
    """Classification labels for rows in the ``positions`` table."""

    ACTUAL_OPEN = "ACTUAL_OPEN"
    MISMATCH_QTY = "MISMATCH_QTY"
    ORPHAN_DISABLED = "ORPHAN_DISABLED"
    ORPHAN_UNKNOWN = "ORPHAN_UNKNOWN"
    ORPHAN_NOT_HELD = "ORPHAN_NOT_HELD"
    ACTUAL_FILL = "ACTUAL_FILL"
    PHANTOM_CLOSE = "PHANTOM_CLOSE"
    CLOSED_NO_EXIT = "CLOSED_NO_EXIT"


class TradeClass(str, Enum):
    """Classification labels for rows in the ``trades`` table."""

    ENTRY_ONLY_PHANTOM = "ENTRY_ONLY_PHANTOM"
    MISSING_STRATEGY = "MISSING_STRATEGY"
    MISSING_EXIT = "MISSING_EXIT"
    COMPLETE = "COMPLETE"


_POSITION_ACTION: dict[PositionClass, str] = {
    PositionClass.ACTUAL_OPEN: "No action — DB and Alpaca agree.",
    PositionClass.MISMATCH_QTY: (
        "Update positions.quantity to match Alpaca (or place reconciling order). "
        "Investigate whether a partial fill was missed."
    ),
    PositionClass.ORPHAN_DISABLED: (
        "Adopt-and-flatten: re-enable the strategy long enough for the next tick "
        "to manage out, OR add an orphan-handler that takes ownership at tick "
        "time. Do not edit the DB until cash is reconciled."
    ),
    PositionClass.ORPHAN_UNKNOWN: (
        "Manual review required — no strategy means no exit policy. Inspect "
        "Alpaca for matching symbol; if held, adopt under a fallback strategy "
        "and place a manual exit. If not held, mark CLOSED with "
        "exit_reason='reconciliation_mismatch'."
    ),
    PositionClass.ORPHAN_NOT_HELD: (
        "DB shows open but Alpaca holds zero. Likely an unrecorded fill of an "
        "exit order. Backfill the trades row from Alpaca order history (see "
        "alpaca_backfill) and stamp positions.status = CLOSED."
    ),
    PositionClass.ACTUAL_FILL: (
        "No action — DB CLOSED and both entry+exit fills present on Alpaca. "
        "If trades row is missing exit data, run alpaca_backfill."
    ),
    PositionClass.PHANTOM_CLOSE: (
        "Confirm with Alpaca that the entry never filled. If so, the position "
        "row is correct as CLOSED but the trades row should be deleted (or "
        "marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't "
        "insert a trades row until entry fill confirms."
    ),
    PositionClass.CLOSED_NO_EXIT: (
        "Entry filled but no exit fill found in lookback window. Widen window "
        "or treat as wind_down/manual close — backfill trades row from "
        "positions and any matching SELL within 14 days. If still nothing, "
        "exit may have happened pre-window."
    ),
}

_TRADE_ACTION: dict[TradeClass, str] = {
    TradeClass.ENTRY_ONLY_PHANTOM: (
        "Live bug: order_manager._create_position_record inserts trades "
        "without strategy_id and never updates exit (positions.id is used as "
        "trade_id, so UPDATE trades WHERE id = ? misses). Either delete and "
        "rewrite via save_trade(), or backfill via alpaca_backfill keyed off "
        "positions row."
    ),
    TradeClass.MISSING_STRATEGY: (
        "Backfill strategy_id from the paired positions row "
        "(JOIN ON ticker AND entry_time)."
    ),
    TradeClass.MISSING_EXIT: (
        "Pair against CLOSED positions row and backfill exit fields from "
        "Alpaca fills (alpaca_backfill handles this)."
    ),
    TradeClass.COMPLETE: "No action — row is fully populated.",
}


# ---------------------------------------------------------------------------
# Findings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PositionFinding:
    db_row: Mapping[str, Any]
    classification: PositionClass
    evidence: str
    proposed_action: str


@dataclass(frozen=True)
class TradeFinding:
    db_row: Mapping[str, Any]
    classification: TradeClass
    evidence: str
    proposed_action: str


# ---------------------------------------------------------------------------
# Position classifier
# ---------------------------------------------------------------------------


def _find_entry_fill(
    state: AlpacaState,
    *,
    symbol: str,
    entry_time: datetime,
    qty: float,
    explicit_order_id: str | None,
) -> AlpacaOrderRec | None:
    """Return the BUY fill that backs a position's entry, if discoverable."""
    if explicit_order_id and explicit_order_id in state.orders_by_id:
        order = state.orders_by_id[explicit_order_id]
        if order.filled_qty > 0:
            return order
    candidates: Iterable[AlpacaOrderRec] = state.fills_by_symbol.get(symbol, ())
    window_start: datetime = entry_time - timedelta(hours=ENTRY_FILL_LOOKBACK_HOURS)
    window_end: datetime = entry_time + timedelta(hours=ENTRY_FILL_LOOKBACK_HOURS)
    best: AlpacaOrderRec | None = None
    for c in candidates:
        if c.side != "buy":
            continue
        if c.filled_at is None or not (window_start <= c.filled_at <= window_end):
            continue
        if c.filled_qty + QTY_TOLERANCE < qty:
            continue
        if best is None or abs((c.filled_at - entry_time).total_seconds()) < abs(
            (best.filled_at - entry_time).total_seconds()  # type: ignore[operator]
        ):
            best = c
    return best


def _find_exit_fill(
    state: AlpacaState,
    *,
    symbol: str,
    entry_time: datetime,
    qty: float,
    bracket_order_ids: tuple[str | None, ...],
) -> AlpacaOrderRec | None:
    """Return the SELL fill that closed a position, if discoverable."""
    for oid in bracket_order_ids:
        if oid and oid in state.orders_by_id:
            order = state.orders_by_id[oid]
            if order.side == "sell" and order.filled_qty > 0:
                return order
    candidates: Iterable[AlpacaOrderRec] = state.fills_by_symbol.get(symbol, ())
    window_start: datetime = entry_time
    window_end: datetime = entry_time + timedelta(days=EXIT_FILL_LOOKBACK_DAYS)
    for c in candidates:
        if c.side != "sell":
            continue
        if c.filled_at is None or not (window_start <= c.filled_at <= window_end):
            continue
        if c.filled_qty + QTY_TOLERANCE < qty:
            continue
        return c
    return None


def _classify_open_position(
    pos: Mapping[str, Any],
    state: AlpacaState,
    strategy_enabled: Mapping[str, bool],
) -> PositionFinding:
    ticker: str = str(pos["ticker"]).upper()
    db_qty: float = to_float(pos.get("quantity")) or 0.0
    strategy_id: Any = pos.get("strategy_id")
    held = state.positions_by_symbol.get(ticker)

    if strategy_id in (None, "", "unknown"):
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.ORPHAN_UNKNOWN,
            evidence=(
                f"strategy_id={strategy_id!r}; "
                f"alpaca_holds={'yes' if held else 'no'}"
            ),
            proposed_action=_POSITION_ACTION[PositionClass.ORPHAN_UNKNOWN],
        )
    if not strategy_enabled.get(strategy_id, False):
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.ORPHAN_DISABLED,
            evidence=(
                f"strategy_id={strategy_id!r} is disabled in config; "
                f"alpaca_holds={'yes' if held else 'no'}, db_qty={db_qty:g}"
            ),
            proposed_action=_POSITION_ACTION[PositionClass.ORPHAN_DISABLED],
        )
    if held is None:
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.ORPHAN_NOT_HELD,
            evidence=(
                f"DB qty={db_qty:g} but Alpaca holds zero {ticker}; "
                f"strategy={strategy_id}"
            ),
            proposed_action=_POSITION_ACTION[PositionClass.ORPHAN_NOT_HELD],
        )
    if abs(held.qty - db_qty) > QTY_TOLERANCE:
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.MISMATCH_QTY,
            evidence=(
                f"DB qty={db_qty:g} vs Alpaca qty={held.qty:g} "
                f"(diff={held.qty - db_qty:+g})"
            ),
            proposed_action=_POSITION_ACTION[PositionClass.MISMATCH_QTY],
        )
    return PositionFinding(
        db_row=pos,
        classification=PositionClass.ACTUAL_OPEN,
        evidence=(
            f"DB qty={db_qty:g} matches Alpaca qty={held.qty:g}; "
            f"avg_entry={held.avg_entry_price:.4f}"
        ),
        proposed_action=_POSITION_ACTION[PositionClass.ACTUAL_OPEN],
    )


def _classify_closed_position(
    pos: Mapping[str, Any],
    state: AlpacaState,
) -> PositionFinding:
    ticker: str = str(pos["ticker"]).upper()
    db_qty: float = to_float(pos.get("quantity")) or 0.0
    entry_time: datetime | None = parse_iso(pos.get("entry_time"))
    if entry_time is None:
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.PHANTOM_CLOSE,
            evidence="entry_time is unparseable; cannot search Alpaca for fills",
            proposed_action=_POSITION_ACTION[PositionClass.PHANTOM_CLOSE],
        )

    entry_fill = _find_entry_fill(
        state,
        symbol=ticker,
        entry_time=entry_time,
        qty=db_qty,
        explicit_order_id=pos.get("alpaca_order_id"),
    )
    if entry_fill is None:
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.PHANTOM_CLOSE,
            evidence=(
                f"No BUY fill found on Alpaca within "
                f"+/-{ENTRY_FILL_LOOKBACK_HOURS}h of "
                f"entry_time={entry_time.isoformat()}; "
                f"alpaca_order_id={pos.get('alpaca_order_id')!r}"
            ),
            proposed_action=_POSITION_ACTION[PositionClass.PHANTOM_CLOSE],
        )

    exit_fill = _find_exit_fill(
        state,
        symbol=ticker,
        entry_time=entry_time,
        qty=db_qty,
        bracket_order_ids=(
            pos.get("alpaca_stop_order_id"),
            pos.get("alpaca_target_order_id"),
            pos.get("alpaca_trail_order_id"),
        ),
    )
    if exit_fill is None:
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.CLOSED_NO_EXIT,
            evidence=(
                f"Entry fill found ({entry_fill.order_id}) but no SELL fill "
                f"within {EXIT_FILL_LOOKBACK_DAYS} days. May have closed "
                "outside lookback window."
            ),
            proposed_action=_POSITION_ACTION[PositionClass.CLOSED_NO_EXIT],
        )

    return PositionFinding(
        db_row=pos,
        classification=PositionClass.ACTUAL_FILL,
        evidence=(
            f"Entry {entry_fill.order_id} filled {entry_fill.filled_qty:g}@"
            f"{entry_fill.filled_avg_price}; "
            f"exit {exit_fill.order_id} filled {exit_fill.filled_qty:g}@"
            f"{exit_fill.filled_avg_price}"
        ),
        proposed_action=_POSITION_ACTION[PositionClass.ACTUAL_FILL],
    )


def classify_position(
    pos: Mapping[str, Any],
    state: AlpacaState,
    strategy_enabled: Mapping[str, bool],
) -> PositionFinding:
    """Classify a single ``positions`` row against live Alpaca state.

    Terminal states (CLOSED, ENTRY_FAILED) take the closed-side branch.
    ENTRY_FAILED is the post-V8 label for what we used to detect heuristically
    as PHANTOM_CLOSE — when we see it we can short-circuit to that classification
    without re-querying Alpaca.
    """
    status: str = str(pos.get("status") or "").upper()
    if status == "ENTRY_FAILED":
        return PositionFinding(
            db_row=pos,
            classification=PositionClass.PHANTOM_CLOSE,
            evidence="status='ENTRY_FAILED' — V8 migration already flagged this row",
            proposed_action=_POSITION_ACTION[PositionClass.PHANTOM_CLOSE],
        )
    if status != "CLOSED":
        return _classify_open_position(pos, state, strategy_enabled)
    return _classify_closed_position(pos, state)


# ---------------------------------------------------------------------------
# Trade classifier
# ---------------------------------------------------------------------------


def classify_trade(
    trade: Mapping[str, Any],
    position_lookup: Mapping[tuple[str, str], Mapping[str, Any]],
) -> TradeFinding:
    """Classify a single ``trades`` row, pairing it against positions."""
    strategy_id: Any = trade.get("strategy_id")
    exit_time: Any = trade.get("exit_time")
    has_strategy: bool = bool(strategy_id) and strategy_id != "unknown"
    has_exit: bool = bool(exit_time)
    paired = position_lookup.get(
        (str(trade["ticker"]).upper(), str(trade.get("entry_time") or ""))
    )
    paired_strategy: Any = paired.get("strategy_id") if paired else None
    paired_status: str = str(paired.get("status") or "").upper() if paired else ""

    if not has_strategy and not has_exit:
        return TradeFinding(
            db_row=trade,
            classification=TradeClass.ENTRY_ONLY_PHANTOM,
            evidence=(
                f"strategy_id IS NULL and exit_time IS NULL; "
                f"paired position strategy={paired_strategy!r} "
                f"status={paired_status or 'NONE'}"
            ),
            proposed_action=_TRADE_ACTION[TradeClass.ENTRY_ONLY_PHANTOM],
        )
    if not has_strategy:
        return TradeFinding(
            db_row=trade,
            classification=TradeClass.MISSING_STRATEGY,
            evidence=(
                f"exit_time={exit_time!r} present but strategy_id NULL; "
                f"paired position strategy={paired_strategy!r}"
            ),
            proposed_action=_TRADE_ACTION[TradeClass.MISSING_STRATEGY],
        )
    if not has_exit:
        return TradeFinding(
            db_row=trade,
            classification=TradeClass.MISSING_EXIT,
            evidence=(
                f"strategy_id={strategy_id!r} present but exit_time NULL; "
                f"paired position status={paired_status or 'NONE'}"
            ),
            proposed_action=_TRADE_ACTION[TradeClass.MISSING_EXIT],
        )
    return TradeFinding(
        db_row=trade,
        classification=TradeClass.COMPLETE,
        evidence=f"strategy={strategy_id} exit_time={exit_time}",
        proposed_action=_TRADE_ACTION[TradeClass.COMPLETE],
    )

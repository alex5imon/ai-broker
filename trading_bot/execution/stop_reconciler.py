"""Naked-position reconciliation: detect DB-open rows without an active broker-side stop.

Walks ``positions`` rows that should have an active protective stop and
verifies the recorded ``alpaca_stop_order_id`` is in an active state at
Alpaca. Surfaces any mismatch as a HIGH-priority notification so the
operator sees the bug class from issue #117 even when the in-tick
recovery (see ``OrderManager._recover_missing_stop``) silently heals it.

This is intended to run once per tick — it's cheap (one Alpaca query
per recorded stop, plus the open-orders scan that the recovery layer
already performs). It does NOT mutate state; it only reports.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alpaca.trading.models import Order as AlpacaOrder

from trading_bot.constants import PositionStatus

if TYPE_CHECKING:
    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)


# Alpaca order statuses that mean the stop is still on the book and
# protective. Anything else (canceled / expired / rejected / filled /
# pending_cancel / suspended) means the position is naked or being
# closed without the protective gate.
_ACTIVE_STOP_STATUSES: frozenset[str] = frozenset(
    {"new", "accepted", "held", "pending_new", "accepted_for_bidding"}
)


@dataclass
class NakedPosition:
    """A DB-tracked open position whose Alpaca stop isn't active."""

    trade_id: int
    ticker: str
    status: str
    quantity: float
    entry_price: float
    stop_price: float
    alpaca_stop_order_id: str | None
    broker_status: str  # 'missing' / 'canceled' / 'expired' / 'rejected' / 'filled' / etc.

    def describe(self) -> str:
        sid: str = self.alpaca_stop_order_id or "NULL"
        return (
            f"{self.ticker} (trade_id={self.trade_id}, qty={self.quantity:.6f}, "
            f"entry=${self.entry_price:.4f}, stop=${self.stop_price:.4f}, "
            f"stop_order_id={sid}, broker_status={self.broker_status})"
        )


@dataclass
class ReconciliationResult:
    """Output of a naked-position scan."""

    rows_checked: int = 0
    naked: list[NakedPosition] = field(default_factory=list)

    @property
    def has_naked(self) -> bool:
        return bool(self.naked)

    def summary(self) -> str:
        if not self.naked:
            return f"Stop reconciliation: {self.rows_checked} rows OK"
        lines: list[str] = [
            f"Stop reconciliation: {len(self.naked)} naked of "
            f"{self.rows_checked} open positions",
        ]
        for n in self.naked:
            lines.append(f"  - {n.describe()}")
        return "\n".join(lines)


# Statuses we expect to have a broker-side stop attached. POSITION_OPEN
# is included on purpose — pre-#117 it could land in the DB without a
# recorded stop_order_id, and the in-tick recovery only fires inside
# the market-hours gate.
_STATUSES_NEEDING_STOP: tuple[str, ...] = (
    PositionStatus.POSITION_OPEN.value,
    PositionStatus.STOP_ACTIVE.value,
    PositionStatus.TRAILING_ACTIVE.value,
)


def _load_open_positions(db_path: str) -> list[sqlite3.Row]:
    """Return positions rows that ought to have an active protective stop."""
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # The f-string composes only literal "?" placeholders from a
            # fixed module-level tuple — no user input reaches the SQL.
            # Bandit B608 flags any f-string in SQL; pinned `# nosec`
            # mirrors the same pattern at order_manager.py:1954.
            placeholders: str = ",".join("?" * len(_STATUSES_NEEDING_STOP))
            rows: list[sqlite3.Row] = conn.execute(
                f"SELECT id, ticker, status, quantity, entry_price, "  # nosec B608
                f"stop_price, alpaca_stop_order_id "
                f"FROM positions WHERE status IN ({placeholders})",
                _STATUSES_NEEDING_STOP,
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning(
            "stop reconciler: positions table unreadable", exc_info=True,
        )
        return []
    return rows


async def _broker_status(
    gateway: GatewayConnection, order_id: str,
) -> str:
    """Return the Alpaca order status for ``order_id``, or 'missing' on lookup
    failure. Failure to fetch is treated as missing so the reconciler errs
    on the side of surfacing potential naked positions."""
    try:
        order: AlpacaOrder = await asyncio.to_thread(
            gateway.client.get_order_by_id, order_id,  # type: ignore[arg-type]
        )
    except Exception:
        logger.debug(
            "stop reconciler: get_order_by_id failed for %s",
            order_id, exc_info=True,
        )
        return "missing"
    status_obj = getattr(order, "status", None)
    return (getattr(status_obj, "value", "") or "").lower() or "unknown"


async def reconcile_open_position_stops(
    db_path: str,
    gateway: GatewayConnection,
    notifier: Notifier | None = None,
) -> ReconciliationResult:
    """Scan DB-open positions and flag any without an active broker stop.

    Args:
        db_path: path to the SQLite DB.
        gateway: connected Alpaca gateway (used for ``get_order_by_id``).
        notifier: optional notifier — when provided, a HIGH-priority
            notification fires for any naked position discovered.

    Returns:
        ReconciliationResult with the list of naked positions. Safe to
        log/discard on every tick; the heavy lifting is one Alpaca
        round-trip per recorded stop (typically ≤ 8 positions in Phase 3).
    """
    rows: list[sqlite3.Row] = _load_open_positions(db_path)
    result: ReconciliationResult = ReconciliationResult(rows_checked=len(rows))

    for row in rows:
        stop_oid: str | None = row["alpaca_stop_order_id"]
        # POSITION_OPEN-with-no-recorded-stop is itself a naked finding —
        # the in-tick recovery may have just attached one, but if this
        # check runs before the recovery branch (or outside the
        # market-hours window) the row is still naked at the broker.
        if not stop_oid:
            naked: NakedPosition = NakedPosition(
                trade_id=int(row["id"]),
                ticker=str(row["ticker"]),
                status=str(row["status"]),
                quantity=float(row["quantity"] or 0),
                entry_price=float(row["entry_price"] or 0),
                stop_price=float(row["stop_price"] or 0),
                alpaca_stop_order_id=None,
                broker_status="missing",
            )
            result.naked.append(naked)
            continue

        broker_status: str = await _broker_status(gateway, stop_oid)
        if broker_status in _ACTIVE_STOP_STATUSES:
            continue
        result.naked.append(
            NakedPosition(
                trade_id=int(row["id"]),
                ticker=str(row["ticker"]),
                status=str(row["status"]),
                quantity=float(row["quantity"] or 0),
                entry_price=float(row["entry_price"] or 0),
                stop_price=float(row["stop_price"] or 0),
                alpaca_stop_order_id=stop_oid,
                broker_status=broker_status,
            )
        )

    if result.has_naked:
        logger.warning(
            "stop reconciler: %d naked position(s) of %d checked",
            len(result.naked), result.rows_checked,
        )
        for naked in result.naked:
            logger.warning("  naked: %s", naked.describe())
        if notifier is not None:
            body: str = "\n".join(n.describe() for n in result.naked)
            await notifier.send(
                "Naked Position Detected",
                (
                    f"Found {len(result.naked)} DB-open "
                    f"position(s) without an active broker-side stop. "
                    f"Issue #117 surface — in-tick recovery should "
                    f"heal during market hours; verify and "
                    f"investigate lineage.\n\n{body}"
                ),
                priority=4,
                tags=["warning"],
            )
    else:
        logger.info(
            "stop reconciler: all %d open position(s) have active stops",
            result.rows_checked,
        )

    return result

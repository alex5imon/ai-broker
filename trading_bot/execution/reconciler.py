"""Phase 1 reconciler — broker-truth desired-state derivation, in SHADOW mode.

This is **Phase 1** of the broker-truth reconciliation-loop design
(``trading_bot/docs/research/reconciliation_loop_design.md``, PR #191; Phase 0
shipped in #192). It builds the two primitives the eventual controller loop is
made of:

1. :func:`broker_snapshot` — the single source of truth for a tick (Alpaca
   positions + open orders), reusing the :class:`BrokerView` snapshot type
   from the Phase 0 guard.
2. :func:`derive_desired_state` — a **pure** function mapping
   ``(intent, broker)`` to the disposition the reconciler *would* drive, one
   row per the design's §3 table.

In Phase 1 these only **observe**: :func:`run_shadow_reconcile` derives a
disposition for every position each tick and logs where the derived state
**disagrees** with the live SQLite state machine (the DB ``status`` column).
**No behaviour changes** and **nothing is paged** — Phase 0's invariant guard
already owns alerting. The shadow log quantifies how well the reconciler's
brain matches reality before Phase 2 lets it drive the ``status`` column.

The §3 disposition table this implements:

| Broker says            | Intent (DB status)       | Desired state    |
|------------------------|--------------------------|------------------|
| held, has stop         | open                     | ``PROTECTED``    |
| held, no stop          | open                     | ``NEEDS_STOP``   |
| held                   | exit requested (CLOSING) | ``FLATTEN``      |
| not held               | open / CLOSING           | ``CLOSED``       |
| held                   | entry pending            | ``OPEN`` (adopt) |
| not held               | entry pending            | ``PENDING_ENTRY``|
| held, no matching row  | (none)                   | ``ADOPT``        |

:func:`derive_desired_state` and :func:`derive_disposition` are pure and are
the unit-test surface.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

from trading_bot.constants import PositionStatus
from trading_bot.execution.invariant_guard import BrokerView

if TYPE_CHECKING:
    from trading_bot.gateway import GatewayConnection

logger: logging.Logger = logging.getLogger(__name__)


# DB statuses asserting "this position should be held at the broker now".
_OPEN_STATUSES: frozenset[str] = frozenset(
    {
        PositionStatus.POSITION_OPEN.value,
        PositionStatus.STOP_ACTIVE.value,
        PositionStatus.TRAILING_ACTIVE.value,
    }
)


class DesiredState(str, Enum):
    """The disposition the reconciler would drive a position toward.

    Derived purely from broker truth + the recorded intent — never
    transitioned locally. Phase 1 only logs these; Phase 2 acts on them.
    """

    PROTECTED = "PROTECTED"        # held + protective stop, intent open
    NEEDS_STOP = "NEEDS_STOP"      # held, no stop on book, intent open
    FLATTEN = "FLATTEN"            # intent CLOSING, broker still holds it
    CLOSED = "CLOSED"              # not held, intent open/closing -> exit done
    OPEN = "OPEN"                  # entry filled (held) while intent pending
    PENDING_ENTRY = "PENDING_ENTRY"  # entry order still working, not held
    ADOPT = "ADOPT"               # broker holds it, no DB intent row
    UNKNOWN = "UNKNOWN"           # unexpected DB status — investigate


# Human-readable convergence action the reconciler WOULD take per state. Used
# only for the shadow log in Phase 1; Phase 2 wires these to real handlers.
_ACTION_BY_STATE: dict[DesiredState, str] = {
    DesiredState.PROTECTED: "none (protected)",
    DesiredState.NEEDS_STOP: "attach stop (idempotent)",
    DesiredState.FLATTEN: "re-submit flatten iff RTH + no live exit order",
    DesiredState.CLOSED: "journal exit from broker truth",
    DesiredState.OPEN: "adopt broker qty/price -> POSITION_OPEN",
    DesiredState.PENDING_ENTRY: "none (entry order working)",
    DesiredState.ADOPT: "adopt unknown broker position / investigate lineage",
    DesiredState.UNKNOWN: "investigate (unexpected DB status)",
}


@dataclass(frozen=True)
class PositionIntent:
    """What the DB says we *meant* to hold for one ticker (the intent journal).

    In the reconciliation-loop model the DB row is an intent record, not an
    authoritative state machine. Phase 1 reads only the fields needed to
    derive a disposition; later phases will carry stop/target prices too.
    """

    trade_id: int
    ticker: str
    status: str
    quantity: float
    strategy_id: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> PositionIntent:
        return cls(
            trade_id=int(row.get("id", 0) or 0),
            ticker=str(row.get("ticker", "") or ""),
            status=str(row.get("status", "") or ""),
            quantity=float(row.get("quantity", 0) or 0),
            strategy_id=(
                str(row["strategy_id"]) if row.get("strategy_id") else None
            ),
        )


@dataclass(frozen=True)
class Disposition:
    """A derived desired-state for one ticker, plus the shadow comparison."""

    ticker: str
    intent_status: str | None  # DB status; None for an ADOPT (no DB row)
    desired: DesiredState
    action: str
    agrees_with_db: bool       # would the reconciler leave the DB untouched?

    def describe(self) -> str:
        intent: str = self.intent_status or "(no DB row)"
        flag: str = "OK " if self.agrees_with_db else "DIFF"
        return (
            f"{flag} {self.ticker}: db={intent} -> desired={self.desired.value} "
            f"| would: {self.action}"
        )


def derive_desired_state(intent: PositionIntent, broker: BrokerView) -> DesiredState:
    """Pure derivation of the desired disposition for one position.

    Implements the design's §3 table from broker truth (held / has-stop) and
    the recorded DB status. No side effects — the unit-test surface.
    """
    held: bool = broker.is_held(intent.ticker)
    has_stop: bool = broker.has_stop(intent.ticker)
    status: str = intent.status

    if status == PositionStatus.ENTRY_PENDING.value:
        return DesiredState.OPEN if held else DesiredState.PENDING_ENTRY

    if status == PositionStatus.CLOSING.value:
        return DesiredState.FLATTEN if held else DesiredState.CLOSED

    if status in _OPEN_STATUSES:
        if not held:
            return DesiredState.CLOSED
        return DesiredState.PROTECTED if has_stop else DesiredState.NEEDS_STOP

    # Terminal statuses are never loaded as intents; anything else is an
    # unexpected status the reconciler should surface rather than silently map.
    return DesiredState.UNKNOWN


def _db_reflects(desired: DesiredState, status: str) -> bool:
    """True iff the live DB status already matches the derived disposition.

    Agreement means "the reconciler would make no change" — so disagreements
    are exactly the cases Phase 2 would act on. PROTECTED agrees with any open
    status (the POSITION_OPEN-vs-STOP_ACTIVE distinction is cosmetic when the
    broker stop is on the book); FLATTEN agrees with CLOSING (intent already
    recorded, action merely pending). NEEDS_STOP / CLOSED / OPEN / UNKNOWN
    never have a status that "reflects" them on a non-terminal row.
    """
    if desired == DesiredState.PROTECTED:
        return status in _OPEN_STATUSES
    if desired == DesiredState.PENDING_ENTRY:
        return status == PositionStatus.ENTRY_PENDING.value
    if desired == DesiredState.FLATTEN:
        return status == PositionStatus.CLOSING.value
    return False


def derive_disposition(intent: PositionIntent, broker: BrokerView) -> Disposition:
    """Derive the full :class:`Disposition` (state + action + agreement)."""
    desired: DesiredState = derive_desired_state(intent, broker)
    return Disposition(
        ticker=intent.ticker,
        intent_status=intent.status,
        desired=desired,
        action=_ACTION_BY_STATE[desired],
        agrees_with_db=_db_reflects(desired, intent.status),
    )


def derive_adopt(ticker: str, broker: BrokerView) -> Disposition:
    """Disposition for a broker position with no matching open DB row."""
    return Disposition(
        ticker=ticker,
        intent_status=None,
        desired=DesiredState.ADOPT,
        action=_ACTION_BY_STATE[DesiredState.ADOPT],
        agrees_with_db=False,
    )


def derive_all_dispositions(
    intents: list[PositionIntent], broker: BrokerView
) -> list[Disposition]:
    """Pure: derive a disposition for every intent + every unmatched broker hold."""
    dispositions: list[Disposition] = [
        derive_disposition(intent, broker) for intent in intents
    ]
    matched: set[str] = {intent.ticker for intent in intents}
    for ticker in broker.held_qty:
        if ticker not in matched:
            dispositions.append(derive_adopt(ticker, broker))
    # Disagreements first (agrees_with_db False < True), then by ticker.
    dispositions.sort(key=lambda d: (d.agrees_with_db, d.ticker))
    return dispositions


@dataclass
class ShadowReconcileResult:
    """Outcome of one shadow-mode reconcile pass."""

    dispositions: list[Disposition] = field(default_factory=list)

    @property
    def disagreements(self) -> list[Disposition]:
        return [d for d in self.dispositions if not d.agrees_with_db]

    @property
    def agreement_count(self) -> int:
        return sum(1 for d in self.dispositions if d.agrees_with_db)

    def summary(self) -> str:
        total: int = len(self.dispositions)
        diff: int = len(self.disagreements)
        head: str = (
            f"shadow reconcile: {total - diff}/{total} dispositions agree with "
            f"the live state machine ({diff} would change)"
        )
        if not self.dispositions:
            return "shadow reconcile: no open positions"
        lines: list[str] = [head]
        for d in self.dispositions:
            lines.append(f"  {d.describe()}")
        return "\n".join(lines)


def _load_intents(db_path: str) -> list[PositionIntent]:
    """Load every non-terminal ``positions`` row as a :class:`PositionIntent`."""
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT id, ticker, status, quantity, strategy_id "
                "FROM positions WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED')"
            )
            return [PositionIntent.from_row(dict(row)) for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning("shadow reconcile: positions table unreadable", exc_info=True)
        return []


async def broker_snapshot(gateway: GatewayConnection) -> BrokerView:
    """Fetch the broker's truth for this tick (positions + open orders).

    One ``get_positions`` + one ``get_open_orders`` round-trip — the single
    source of truth the reconciler derives from.
    """
    positions = await gateway.get_positions()
    orders = await gateway.get_open_orders()
    return BrokerView.from_alpaca(positions, orders)


async def run_shadow_reconcile(
    db_path: str,
    gateway: GatewayConnection,
) -> ShadowReconcileResult:
    """Run the Phase 1 reconciler in SHADOW mode for one tick (read-only).

    Derives a desired disposition for every open position and logs where the
    derivation disagrees with the live SQLite state machine. Takes **no**
    action and pages **nothing** (Phase 0's guard owns alerting). Safe to call
    every tick and ignore the result.
    """
    broker: BrokerView = await broker_snapshot(gateway)
    intents: list[PositionIntent] = await asyncio.to_thread(_load_intents, db_path)

    dispositions: list[Disposition] = derive_all_dispositions(intents, broker)
    result: ShadowReconcileResult = ShadowReconcileResult(dispositions=dispositions)

    logger.info("%s", result.summary())
    if result.disagreements:
        logger.warning(
            "shadow reconcile: %d disposition(s) would change under the "
            "reconciler (Phase 1 is observe-only — no action taken):\n%s",
            len(result.disagreements),
            "\n".join(f"  - {d.describe()}" for d in result.disagreements),
        )
    return result

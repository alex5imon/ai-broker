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
    trade_id: int | None = None  # DB positions.id; None for an ADOPT

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
        trade_id=intent.trade_id,
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


# ---------------------------------------------------------------------------
# Phase 2 — convergence (status ownership), gated by the `reconciler.drive`
# flag. With the flag OFF (default) this layer plans + logs but executes
# nothing: behaviour is identical to Phase 1 shadow mode. With it ON, the
# reconciler OWNS the DB `status` column for the *lossless, DB-only*
# transitions it can safely drive from positive broker evidence; every
# order-submitting or P&L-bearing action is delegated to the existing healers
# (StateRecovery / OrderManager) and only logged here. Deleting those reactive
# patches is Phase 2b, gated on shadow-agreement evidence (design §5).
# ---------------------------------------------------------------------------


class ConvergenceOp(str, Enum):
    """The kind of convergence step a disposition implies."""

    NONE = "NONE"                          # already converged — nothing to do
    NORMALIZE_STATUS = "NORMALIZE_STATUS"  # reconciler-owned lossless DB write
    ATTACH_STOP = "ATTACH_STOP"            # delegated: submit protective stop
    FLATTEN = "FLATTEN"                    # delegated: submit flatten (RTH)
    JOURNAL_CLOSED = "JOURNAL_CLOSED"      # delegated: exit journal + P&L
    ADOPT = "ADOPT"                        # delegated: adopt broker qty/price
    INVESTIGATE = "INVESTIGATE"            # unexpected — alert only


# The ONLY op the reconciler executes itself when drive is on. It is lossless
# (a status label change), idempotent, and fires only on positive broker
# evidence (held + protective stop on the book) — so a transient/empty broker
# read can never trigger it. Every other op is owned by the existing healers.
_RECONCILER_OWNED: frozenset[ConvergenceOp] = frozenset({ConvergenceOp.NORMALIZE_STATUS})


@dataclass(frozen=True)
class ConvergenceAction:
    """A single planned convergence step for one ticker."""

    ticker: str
    trade_id: int | None
    desired: DesiredState
    op: ConvergenceOp
    target_status: str | None  # only set for NORMALIZE_STATUS
    detail: str

    @property
    def reconciler_owned(self) -> bool:
        return self.op in _RECONCILER_OWNED

    def describe(self) -> str:
        owner: str = "reconciler" if self.reconciler_owned else "healer"
        return f"[{self.op.value}/{owner}] {self.ticker}: {self.detail}"


def _plan_one(disp: Disposition) -> ConvergenceAction:
    """Pure: map one :class:`Disposition` to its convergence action."""
    d: DesiredState = disp.desired

    if d == DesiredState.PROTECTED:
        # Lossless normalization: a held+stopped position recorded as
        # POSITION_OPEN should read STOP_ACTIVE. Fires only on positive broker
        # evidence, so it is safe against transient/empty reads. Any other
        # open status already reflects reality -> NONE.
        if disp.intent_status == PositionStatus.POSITION_OPEN.value:
            return ConvergenceAction(
                ticker=disp.ticker, trade_id=disp.trade_id, desired=d,
                op=ConvergenceOp.NORMALIZE_STATUS,
                target_status=PositionStatus.STOP_ACTIVE.value,
                detail="normalize POSITION_OPEN -> STOP_ACTIVE (broker stop confirmed)",
            )
        return ConvergenceAction(
            ticker=disp.ticker, trade_id=disp.trade_id, desired=d,
            op=ConvergenceOp.NONE, target_status=None,
            detail="already protected",
        )

    op_by_state: dict[DesiredState, tuple[ConvergenceOp, str]] = {
        DesiredState.NEEDS_STOP: (
            ConvergenceOp.ATTACH_STOP,
            "attach protective stop (delegated to OrderManager recovery)",
        ),
        DesiredState.FLATTEN: (
            ConvergenceOp.FLATTEN,
            "re-submit flatten iff RTH (delegated to existing exit path)",
        ),
        DesiredState.CLOSED: (
            ConvergenceOp.JOURNAL_CLOSED,
            "journal exit from broker truth (delegated to StateRecovery/poll)",
        ),
        DesiredState.OPEN: (
            ConvergenceOp.ADOPT,
            "adopt broker qty/price (delegated to _transition_to_open)",
        ),
        DesiredState.ADOPT: (
            ConvergenceOp.ADOPT,
            "adopt unknown broker position (delegated to StateRecovery)",
        ),
        DesiredState.UNKNOWN: (
            ConvergenceOp.INVESTIGATE,
            "unexpected DB status — investigate",
        ),
        DesiredState.PENDING_ENTRY: (
            ConvergenceOp.NONE,
            "entry order still working",
        ),
    }
    op, detail = op_by_state[d]
    return ConvergenceAction(
        ticker=disp.ticker, trade_id=disp.trade_id, desired=d,
        op=op, target_status=None, detail=detail,
    )


def plan_convergence(dispositions: list[Disposition]) -> list[ConvergenceAction]:
    """Pure: turn dispositions into the convergence plan (no side effects).

    Actions that are no-ops (``NONE``) are dropped — the plan lists only the
    steps that would change something. Reconciler-owned actions sort first.
    """
    actions: list[ConvergenceAction] = [
        a for a in (_plan_one(d) for d in dispositions) if a.op != ConvergenceOp.NONE
    ]
    actions.sort(key=lambda a: (not a.reconciler_owned, a.ticker))
    return actions


def _write_status(db_path: str, trade_id: int, status: str) -> bool:
    """Idempotently set a non-terminal position's status. Returns True on write.

    Guarded to never touch a terminal row (CLOSED/ENTRY_FAILED) — the
    reconciler must not resurrect a closed position.
    """
    from trading_bot.constants import TERMINAL_POSITION_STATUSES

    placeholders: str = ",".join("?" * len(TERMINAL_POSITION_STATUSES))
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                # nosec B608 — placeholders are literal "?" from a fixed
                # module-level constant; no user input reaches the SQL.
                f"UPDATE positions SET status = ?, "  # nosec B608
                f"updated_at = datetime('now') "
                f"WHERE id = ? AND status NOT IN ({placeholders})",
                (status, trade_id, *sorted(TERMINAL_POSITION_STATUSES)),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning(
            "reconcile: failed to write status for trade_id=%s", trade_id,
            exc_info=True,
        )
        return False


async def converge(
    db_path: str,
    plan: list[ConvergenceAction],
    *,
    drive: bool,
) -> list[ConvergenceAction]:
    """Execute the reconciler-owned subset of the plan iff ``drive`` is on.

    Returns the list of actions actually executed (empty when ``drive`` is
    off, or when the plan has no reconciler-owned steps). Healer-delegated
    actions are never executed here — the existing in-tick healers do that
    work; this layer only plans and logs them.
    """
    if not drive:
        return []
    executed: list[ConvergenceAction] = []
    for action in plan:
        if action.op != ConvergenceOp.NORMALIZE_STATUS or action.trade_id is None:
            continue
        assert action.target_status is not None  # NORMALIZE_STATUS invariant
        wrote: bool = await asyncio.to_thread(
            _write_status, db_path, action.trade_id, action.target_status,
        )
        if wrote:
            executed.append(action)
            logger.info("reconcile: drove %s", action.describe())
    return executed


@dataclass
class ShadowReconcileResult:
    """Outcome of one reconcile pass (shadow, or driven when the flag is on)."""

    dispositions: list[Disposition] = field(default_factory=list)
    plan: list[ConvergenceAction] = field(default_factory=list)
    executed: list[ConvergenceAction] = field(default_factory=list)
    drive: bool = False

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


async def run_reconcile(
    db_path: str,
    gateway: GatewayConnection,
    *,
    drive: bool = False,
) -> ShadowReconcileResult:
    """Run one reconcile pass for a tick.

    Always derives a desired disposition for every open position and logs
    where the derivation disagrees with the live SQLite state machine
    (shadow). It then plans the convergence steps; when ``drive`` is **on**,
    the reconciler-owned subset (lossless status normalization) is executed
    and the DB ``status`` column is owned by broker truth. When ``drive`` is
    **off** (the default), nothing is executed — behaviour is identical to
    Phase 1 shadow mode.

    Order-submitting / P&L-bearing actions (attach stop, flatten, journal
    exit, adopt) are **never** executed here — they are planned and logged,
    and the existing in-tick healers do the work. Pages nothing (Phase 0's
    guard owns alerting). Safe to call every tick and ignore the result.
    """
    broker: BrokerView = await broker_snapshot(gateway)
    intents: list[PositionIntent] = await asyncio.to_thread(_load_intents, db_path)

    dispositions: list[Disposition] = derive_all_dispositions(intents, broker)
    plan: list[ConvergenceAction] = plan_convergence(dispositions)
    result: ShadowReconcileResult = ShadowReconcileResult(
        dispositions=dispositions, plan=plan, drive=drive,
    )

    logger.info("%s", result.summary())
    if result.disagreements:
        mode: str = "drive" if drive else "shadow"
        logger.warning(
            "reconcile (%s): %d disposition(s) diverge from the live state "
            "machine:\n%s",
            mode,
            len(result.disagreements),
            "\n".join(f"  - {d.describe()}" for d in result.disagreements),
        )

    result.executed = await converge(db_path, plan, drive=drive)

    # Surface the steps the reconciler did NOT own (delegated to healers) so
    # the operator can confirm the healers are converging them.
    delegated: list[ConvergenceAction] = [
        a for a in plan if not a.reconciler_owned
    ]
    if delegated:
        logger.info(
            "reconcile: %d action(s) delegated to existing healers "
            "(not driven by the reconciler):\n%s",
            len(delegated),
            "\n".join(f"  - {a.describe()}" for a in delegated),
        )
    return result


async def run_shadow_reconcile(
    db_path: str,
    gateway: GatewayConnection,
) -> ShadowReconcileResult:
    """Backward-compatible shadow entry point — :func:`run_reconcile` with
    ``drive=False``. Retained for callers that only want observe-only mode."""
    return await run_reconcile(db_path, gateway, drive=False)

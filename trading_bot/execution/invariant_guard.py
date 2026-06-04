"""Phase 0 invariant guard — a per-tick, observer-only consistency check.

This is **Phase 0** of the broker-truth reconciliation-loop design
(``trading_bot/docs/research/reconciliation_loop_design.md``, PR #191). It
catches the *entire* DB↔broker lifecycle bug class **as a class**, with one
assertion per drift mode, instead of one reactive point-fix per new way the
local SQLite state machine can diverge from Alpaca.

It **takes no corrective action** — it reads broker truth (positions + open
orders) and the DB-open rows, derives every position's consistency, and
*reports* divergences. The existing healers (``StateRecovery``,
``reconcile_open_position_stops``, ``_check_order_statuses``) keep doing the
actual repair. The guard's job is **early-warning + evidence-gathering**: run
it for ~1 week in paper to quantify how often each divergence really fires
before the later phases let the reconciler drive behaviour.

Four invariants, each mapping to a historical incident class:

| Invariant | Condition | Caught by point-fixes |
|---|---|---|
| ``naked``   | broker holds it, **no** protective stop on the book | #118 / #169 |
| ``wedge``   | DB row ``CLOSING`` but broker **still holds** it     | #190        |
| ``orphan``  | DB row open-ish but broker **does not** hold it      | #65–#70     |
| ``unknown`` | broker holds a ticker with **no** open DB row        | orphan attr |

**>1-tick persistence gate.** Several of these states are *legitimate* for a
single tick — a fresh fill lags Alpaca's positions endpoint, an exit order is
in flight, an overnight_drift entry hasn't had its standalone stop attached
yet. So a violation only **escalates** to a CRITICAL log + push notification
once it has been seen on **two consecutive ticks**. The first sighting is
logged at INFO and remembered (in ``tick_state`` under
``_INVARIANT_GUARD_KEY``); transient one-tick blips self-resolve and never
page the operator. This mirrors the design's "inconsistent for > 1 tick" rule.

The guard is pure where it matters: :func:`derive_violations` is a side-effect-
free function of (broker view, DB rows) and is the unit-test surface.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from trading_bot.constants import PositionStatus, TERMINAL_POSITION_STATUSES

if TYPE_CHECKING:
    from alpaca.trading.models import Order as AlpacaOrder
    from alpaca.trading.models import Position as AlpacaPosition

    from trading_bot.gateway import GatewayConnection
    from trading_bot.notifications import Notifier

logger: logging.Logger = logging.getLogger(__name__)


# A position smaller than this (in shares) is treated as flat. Alpaca holds
# fractional quantities down to 1/1e6, so an int() truncation would mask a
# real sub-1-share holding — compare with a float tolerance instead.
_QTY_EPSILON: float = 1e-6

# tick_state key under which the guard journals the currently-violating
# (kind:ticker) set, so the next tick can tell a persistent divergence from a
# transient one-tick blip.
_INVARIANT_GUARD_KEY: str = "__invariant_guard__"

# Alpaca order types that count as a protective stop on the book. Matches the
# convention in ``StateRecovery._verify_stop_orders`` (stop / trailing_stop)
# plus stop_limit, since a stop-limit exit is equally protective.
_PROTECTIVE_ORDER_TYPES: frozenset[str] = frozenset(
    {"stop", "stop_limit", "trailing_stop"}
)

# DB statuses that assert "this position should be held at the broker right
# now". ENTRY_PENDING is deliberately excluded — it has not filled yet, so the
# broker legitimately does not hold it (that is not an orphan). CLOSING is
# excluded here because it is handled by the dedicated ``wedge`` invariant.
_HELD_EXPECTED_STATUSES: frozenset[str] = frozenset(
    {
        PositionStatus.POSITION_OPEN.value,
        PositionStatus.STOP_ACTIVE.value,
        PositionStatus.TRAILING_ACTIVE.value,
    }
)

# Invariant kind identifiers (stable strings — used as journal keys).
KIND_NAKED: str = "naked"
KIND_WEDGE: str = "wedge"
KIND_ORPHAN: str = "orphan"
KIND_UNKNOWN: str = "unknown"


@dataclass(frozen=True)
class BrokerView:
    """An immutable snapshot of the broker's truth for one tick.

    ``held_qty`` maps ticker -> signed share quantity for every position the
    broker actually holds (|qty| > epsilon). ``tickers_with_stop`` is the set
    of tickers with at least one protective stop/stop_limit/trailing_stop
    order resting on the book.
    """

    held_qty: dict[str, float]
    tickers_with_stop: frozenset[str]

    def is_held(self, ticker: str) -> bool:
        return abs(self.held_qty.get(ticker, 0.0)) > _QTY_EPSILON

    def has_stop(self, ticker: str) -> bool:
        return ticker in self.tickers_with_stop

    @classmethod
    def from_alpaca(
        cls,
        positions: list[AlpacaPosition],
        orders: list[AlpacaOrder],
    ) -> BrokerView:
        """Build a :class:`BrokerView` from raw Alpaca position/order lists."""
        held: dict[str, float] = {}
        for pos in positions:
            ticker: str = str(pos.symbol)
            qty: float = float(pos.qty or 0)
            if abs(qty) > _QTY_EPSILON:
                held[ticker] = qty

        with_stop: set[str] = set()
        for order in orders:
            order_type = getattr(order, "type", None)
            type_val: str = (getattr(order_type, "value", "") or "").lower()
            if type_val in _PROTECTIVE_ORDER_TYPES:
                with_stop.add(str(order.symbol))

        return cls(held_qty=dict(held), tickers_with_stop=frozenset(with_stop))


@dataclass(frozen=True)
class InvariantViolation:
    """A single DB↔broker inconsistency found on one tick."""

    kind: str  # one of KIND_NAKED / KIND_WEDGE / KIND_ORPHAN / KIND_UNKNOWN
    ticker: str
    detail: str

    @property
    def key(self) -> str:
        """Stable identity used for cross-tick persistence tracking."""
        return f"{self.kind}:{self.ticker}"

    def describe(self) -> str:
        return f"[{self.kind}] {self.ticker}: {self.detail}"


@dataclass
class GuardResult:
    """Outcome of one guard run."""

    rows_checked: int = 0
    broker_positions: int = 0
    violations: list[InvariantViolation] = field(default_factory=list)
    # Subset of ``violations`` seen on the previous tick too (> 1 tick) — the
    # ones that escalate to CRITICAL + notification.
    escalated: list[InvariantViolation] = field(default_factory=list)

    @property
    def has_violations(self) -> bool:
        return bool(self.violations)

    def summary(self) -> str:
        if not self.violations:
            return (
                f"invariant guard: {self.rows_checked} DB rows / "
                f"{self.broker_positions} broker positions consistent"
            )
        lines: list[str] = [
            f"invariant guard: {len(self.violations)} violation(s) "
            f"({len(self.escalated)} persistent) across "
            f"{self.rows_checked} DB rows / {self.broker_positions} broker "
            f"positions:",
        ]
        for v in self.violations:
            persisted: str = " (PERSISTENT)" if v in self.escalated else ""
            lines.append(f"  - {v.describe()}{persisted}")
        return "\n".join(lines)


def derive_violations(
    broker: BrokerView,
    db_rows: list[dict[str, Any]],
) -> list[InvariantViolation]:
    """Pure derivation of every DB↔broker invariant violation for one tick.

    Args:
        broker: the broker truth snapshot for this tick.
        db_rows: DB ``positions`` rows in any non-terminal state (the caller
            loads ``status NOT IN (CLOSED, ENTRY_FAILED)``). Each row is a
            mapping with at least ``ticker`` and ``status``.

    Returns:
        A deterministic list of :class:`InvariantViolation`, ordered by kind
        then ticker, with no side effects. This is the unit-test surface.
    """
    violations: list[InvariantViolation] = []

    # Index DB rows by ticker for the "broker holds an unknown ticker" check.
    # A ticker is "known" if any non-terminal row references it.
    open_tickers: set[str] = set()
    for row in db_rows:
        status: str = str(row.get("status", "") or "")
        if status in TERMINAL_POSITION_STATUSES:
            continue
        ticker: str = str(row.get("ticker", "") or "")
        if not ticker:
            continue
        open_tickers.add(ticker)

        held: bool = broker.is_held(ticker)

        # ORPHAN — DB says we hold it, broker disagrees.
        if status in _HELD_EXPECTED_STATUSES and not held:
            violations.append(
                InvariantViolation(
                    kind=KIND_ORPHAN,
                    ticker=ticker,
                    detail=(
                        f"DB status={status} but broker does not hold it "
                        f"(expected an open position)"
                    ),
                )
            )
            continue

        # WEDGE — exit requested (CLOSING) but broker still holds it.
        if status == PositionStatus.CLOSING.value and held:
            qty: float = broker.held_qty.get(ticker, 0.0)
            violations.append(
                InvariantViolation(
                    kind=KIND_WEDGE,
                    ticker=ticker,
                    detail=(
                        f"DB status=CLOSING but broker still holds "
                        f"qty={qty:.6f} (exit never completed)"
                    ),
                )
            )
            continue

        # NAKED — broker holds it but there is no protective stop on the book.
        if held and not broker.has_stop(ticker):
            qty = broker.held_qty.get(ticker, 0.0)
            violations.append(
                InvariantViolation(
                    kind=KIND_NAKED,
                    ticker=ticker,
                    detail=(
                        f"broker holds qty={qty:.6f} (DB status={status}) "
                        f"with no protective stop on the book"
                    ),
                )
            )

    # UNKNOWN — broker holds a ticker the DB has no open row for.
    for ticker in broker.held_qty:
        if ticker in open_tickers:
            continue
        qty = broker.held_qty[ticker]
        held_naked: str = (
            "" if broker.has_stop(ticker) else " and no protective stop"
        )
        violations.append(
            InvariantViolation(
                kind=KIND_UNKNOWN,
                ticker=ticker,
                detail=(
                    f"broker holds qty={qty:.6f} with no open DB row"
                    f"{held_naked}"
                ),
            )
        )

    violations.sort(key=lambda v: (v.kind, v.ticker))
    return violations


def _load_db_open_positions(db_path: str) -> list[dict[str, Any]]:
    """Load every non-terminal ``positions`` row as plain dicts."""
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT id, ticker, status, quantity, strategy_id "
                "FROM positions WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED')"
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning("invariant guard: positions table unreadable", exc_info=True)
        return []


def _load_prev_violation_keys(db_path: str) -> set[str]:
    """Return the violation keys journaled on the previous tick."""
    from trading_bot.db import repository as repo

    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        try:
            row: dict[str, Any] | None = repo.load_tick_state(
                conn, _INVARIANT_GUARD_KEY
            )
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning("invariant guard: tick_state unreadable", exc_info=True)
        return set()
    if row is None:
        return set()
    keys = row.get("state", {}).get("violation_keys", [])
    return {str(k) for k in keys}


def _save_violation_keys(db_path: str, keys: set[str]) -> None:
    """Journal the current violation keys for the next tick's persistence gate."""
    from trading_bot.db import repository as repo

    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
        try:
            repo.save_tick_state(
                conn,
                _INVARIANT_GUARD_KEY,
                last_bar_ts=None,
                state={"violation_keys": sorted(keys)},
            )
        finally:
            conn.close()
    except sqlite3.OperationalError:
        logger.warning(
            "invariant guard: failed to journal violation keys", exc_info=True
        )


async def run_invariant_guard(
    db_path: str,
    gateway: GatewayConnection,
    notifier: Notifier | None = None,
) -> GuardResult:
    """Run the Phase 0 observer-only invariant guard for one tick.

    Reads broker truth (one ``get_positions`` + one ``get_open_orders``
    round-trip), loads DB-open rows, derives every invariant violation, and
    escalates those that have now persisted for **two consecutive ticks** to a
    CRITICAL log + (if a notifier is supplied) a push notification. First-tick
    violations are logged at INFO only and remembered.

    Takes **no corrective action** — see the module docstring. Safe to call on
    every tick and to ignore the return value.
    """
    positions: list[AlpacaPosition] = await gateway.get_positions()
    orders: list[AlpacaOrder] = await gateway.get_open_orders()
    broker: BrokerView = BrokerView.from_alpaca(positions, orders)

    db_rows: list[dict[str, Any]] = await asyncio.to_thread(
        _load_db_open_positions, db_path
    )

    violations: list[InvariantViolation] = derive_violations(broker, db_rows)
    result: GuardResult = GuardResult(
        rows_checked=len(db_rows),
        broker_positions=len(broker.held_qty),
        violations=violations,
    )

    prev_keys: set[str] = await asyncio.to_thread(
        _load_prev_violation_keys, db_path
    )
    current_keys: set[str] = {v.key for v in violations}

    # Persistence gate: escalate only what was already violating last tick.
    result.escalated = [v for v in violations if v.key in prev_keys]

    await asyncio.to_thread(_save_violation_keys, db_path, current_keys)

    if not violations:
        logger.info("%s", result.summary())
        return result

    # First-tick (transient) violations: INFO only, no page.
    first_seen: list[InvariantViolation] = [
        v for v in violations if v.key not in prev_keys
    ]
    if first_seen:
        logger.info(
            "invariant guard: %d new violation(s) this tick (not yet "
            "escalated — may be a transient one-tick window):\n%s",
            len(first_seen),
            "\n".join(f"  - {v.describe()}" for v in first_seen),
        )

    if not result.escalated:
        return result

    # Persistent (> 1 tick) violations: loud + paged.
    logger.critical(
        "invariant guard: %d PERSISTENT DB↔broker violation(s) "
        "(inconsistent > 1 tick):\n%s",
        len(result.escalated),
        "\n".join(f"  - {v.describe()}" for v in result.escalated),
    )
    if notifier is not None:
        body: str = "\n".join(v.describe() for v in result.escalated)
        await notifier.send(
            "Broker/DB Invariant Violation",
            (
                f"{len(result.escalated)} position(s) have been inconsistent "
                f"with broker truth for more than one tick. The in-tick "
                f"healers should converge these — investigate any that "
                f"persist.\n\n{body}"
            ),
            priority=4,
            tags=["rotating_light"],
        )

    return result

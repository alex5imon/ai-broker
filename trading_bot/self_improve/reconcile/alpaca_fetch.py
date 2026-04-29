"""Pull positions and orders from Alpaca for reconciliation.

The fetcher is isolated behind a Protocol so tests can build an ``AlpacaState``
by hand without touching the network.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AlpacaPosition:
    """Subset of an alpaca-py Position we need for reconciliation."""

    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float


@dataclass(frozen=True)
class AlpacaOrderRec:
    """Subset of an alpaca-py Order, normalised for matching."""

    order_id: str
    symbol: str
    side: str  # "buy" / "sell"
    status: str
    qty: float
    filled_qty: float
    filled_avg_price: float | None
    filled_at: datetime | None
    submitted_at: datetime | None


@dataclass(frozen=True)
class AlpacaState:
    """Snapshot of Alpaca state at the time the report was generated."""

    account_id: str
    is_paper: bool
    fetched_at: datetime
    positions_by_symbol: Mapping[str, AlpacaPosition]
    orders_by_id: Mapping[str, AlpacaOrderRec]
    fills_by_symbol: Mapping[str, tuple[AlpacaOrderRec, ...]]


class AlpacaFetcher(Protocol):
    """Minimal interface for the bits of alpaca-py we need."""

    def get_account(self) -> Any: ...

    def get_all_positions(self) -> list[Any]: ...

    def get_orders(self, *, filter: Any) -> list[Any]: ...  # noqa: A002


# ---------------------------------------------------------------------------
# Parsers / normalisers
# ---------------------------------------------------------------------------


def parse_iso(value: str | datetime | None) -> datetime | None:
    """Parse ISO timestamps from Alpaca / SQLite into a tz-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    text: str = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt: datetime = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_alpaca_position(raw: Any) -> AlpacaPosition | None:
    symbol: str | None = getattr(raw, "symbol", None)
    qty: float | None = to_float(getattr(raw, "qty", None))
    if symbol is None or qty is None:
        return None
    return AlpacaPosition(
        symbol=str(symbol).upper(),
        qty=qty,
        avg_entry_price=to_float(getattr(raw, "avg_entry_price", None)) or 0.0,
        market_value=to_float(getattr(raw, "market_value", None)) or 0.0,
    )


def _normalise_alpaca_order(raw: Any) -> AlpacaOrderRec | None:
    order_id: Any = getattr(raw, "id", None)
    symbol: Any = getattr(raw, "symbol", None)
    side: Any = getattr(raw, "side", None)
    status: Any = getattr(raw, "status", None)
    qty: float | None = to_float(getattr(raw, "qty", None))
    if order_id is None or symbol is None or side is None or qty is None:
        return None
    return AlpacaOrderRec(
        order_id=str(order_id),
        symbol=str(symbol).upper(),
        side=str(side).split(".")[-1].lower(),  # "OrderSide.BUY" -> "buy"
        status=str(status).split(".")[-1].lower(),
        qty=qty,
        filled_qty=to_float(getattr(raw, "filled_qty", None)) or 0.0,
        filled_avg_price=to_float(getattr(raw, "filled_avg_price", None)),
        filled_at=parse_iso(getattr(raw, "filled_at", None)),
        submitted_at=parse_iso(getattr(raw, "submitted_at", None)),
    )


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch_alpaca_state(
    client: AlpacaFetcher,
    *,
    since: datetime,
    until: datetime,
    page_limit: int = 500,
) -> AlpacaState:
    """Pull positions and orders from Alpaca in the [since, until] window.

    Closed and open orders are paginated by ``submitted_at``; both feeds
    are merged into ``orders_by_id`` so callers can look up by order id
    regardless of status.
    """
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    account = client.get_account()
    account_id: str = str(getattr(account, "account_number", ""))

    positions_raw: list[Any] = client.get_all_positions()
    positions_by_symbol: dict[str, AlpacaPosition] = {}
    for raw in positions_raw:
        norm = _normalise_alpaca_position(raw)
        if norm is not None:
            positions_by_symbol[norm.symbol] = norm

    orders_by_id: dict[str, AlpacaOrderRec] = {}
    fills_by_symbol: dict[str, list[AlpacaOrderRec]] = {}

    for status in (QueryOrderStatus.CLOSED, QueryOrderStatus.OPEN):
        cursor_until: datetime = until
        while True:
            request = GetOrdersRequest(
                status=status,
                after=since,
                until=cursor_until,
                limit=page_limit,
                direction="desc",
            )
            page: list[Any] = client.get_orders(filter=request)
            if not page:
                break
            oldest: datetime | None = None
            for raw in page:
                norm = _normalise_alpaca_order(raw)
                if norm is None:
                    continue
                orders_by_id[norm.order_id] = norm
                if norm.filled_at is not None and norm.filled_qty > 0:
                    fills_by_symbol.setdefault(norm.symbol, []).append(norm)
                ts: datetime | None = norm.submitted_at or norm.filled_at
                if ts is not None and (oldest is None or ts < oldest):
                    oldest = ts
            if len(page) < page_limit or oldest is None:
                break
            # Step the cursor strictly older. Alpaca's ``until`` is exclusive,
            # so a 1-microsecond shift is enough to keep paging.
            next_until: datetime = oldest - timedelta(microseconds=1)
            if next_until <= since:
                break
            cursor_until = next_until

    is_paper: bool = os.environ.get("ALPACA_ENV", "paper").strip().lower() != "live"

    return AlpacaState(
        account_id=account_id,
        is_paper=is_paper,
        fetched_at=datetime.now(tz=timezone.utc),
        positions_by_symbol=positions_by_symbol,
        orders_by_id=orders_by_id,
        fills_by_symbol={k: tuple(v) for k, v in fills_by_symbol.items()},
    )

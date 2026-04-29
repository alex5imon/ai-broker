"""Enums, constants, and static mappings for the trading bot."""

from enum import Enum, IntEnum
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Phase progression
# ---------------------------------------------------------------------------

class Phase(IntEnum):
    """Account growth phases. Dictates strategy parameters."""
    CLEANUP = 0
    MICRO = 1
    SMALL = 2
    FULL = 3


# ---------------------------------------------------------------------------
# Market / Exchange / Currency
# ---------------------------------------------------------------------------

class Exchange(str, Enum):
    """Supported exchanges."""
    NYSE = "NYSE"
    NASDAQ = "NASDAQ"


class Currency(str, Enum):
    """Supported currencies."""
    GBP = "GBP"
    USD = "USD"


class Market(str, Enum):
    """Logical market grouping (used for schedule & watchlist selection)."""
    US = "US"


# ---------------------------------------------------------------------------
# Order / Position enums
# ---------------------------------------------------------------------------

class OrderSide(str, Enum):
    """Buy or sell."""
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    """Lifecycle states for a position.

    Terminal states (the row never transitions out):
        - CLOSED — entry filled, then exited (normal lifecycle).
        - ENTRY_FAILED — entry never filled (cancel, reject, timeout). The
          row exists because we optimistically inserted into ``positions``
          before the bracket order confirmed; we cannot delete it without
          breaking foreign keys + audit history. Callers querying for
          "in-flight" positions must exclude both terminal states.

    All other states are in-flight.
    """
    ENTRY_PENDING = "ENTRY_PENDING"
    POSITION_OPEN = "POSITION_OPEN"
    STOP_AND_TARGET_ACTIVE = "STOP_AND_TARGET_ACTIVE"
    TRAILING_ACTIVE = "TRAILING_ACTIVE"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ENTRY_FAILED = "ENTRY_FAILED"


# Set of terminal PositionStatus values. Use this in any query that asks
# "is this position still in flight?" — it must exclude every terminal
# state, not just CLOSED.
TERMINAL_POSITION_STATUSES: frozenset[str] = frozenset(
    {PositionStatus.CLOSED.value, PositionStatus.ENTRY_FAILED.value}
)


class HoldType(str, Enum):
    """How long we intend to hold."""
    INTRADAY = "intraday"
    SWING = "swing"


class ExitReason(str, Enum):
    """Why a position was closed."""
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    TIME_STOP = "time_stop"
    WIND_DOWN = "wind_down"
    KILL_SWITCH = "kill_switch"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    DRAWDOWN_BREAKER = "drawdown_breaker"
    MANUAL = "manual"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    PHASE0_CLEANUP = "phase0_cleanup"


# ---------------------------------------------------------------------------
# Timezone constants
# ---------------------------------------------------------------------------

TZ_EASTERN: ZoneInfo = ZoneInfo("US/Eastern")
TZ_LONDON: ZoneInfo = ZoneInfo("Europe/London")
TZ_UTC: ZoneInfo = ZoneInfo("UTC")


# ---------------------------------------------------------------------------
# GICS sector mapping — every watchlist ticker across all phases
# ---------------------------------------------------------------------------

GICS_SECTOR: dict[str, str] = {
    # Phase 1 — US
    "F": "Consumer Discretionary",
    "AAL": "Industrials",
    "SOFI": "Financials",
    "BAC": "Financials",
    "PLTR": "Information Technology",
    "NIO": "Consumer Discretionary",
    "SNAP": "Communication Services",
    "INTC": "Information Technology",
    # Phase 2 — US
    "XLF": "Financials",
    "XLE": "Energy",
    "XLK": "Information Technology",
    "T": "Communication Services",
    "UBER": "Industrials",
    "XLV": "Health Care",
    # Phase 3 — US
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "NVDA": "Information Technology",
    "GOOGL": "Communication Services",
    "AMZN": "Consumer Discretionary",
    "META": "Communication Services",
    "SQQQ": "Financials",
    "SDS": "Financials",
}


# ---------------------------------------------------------------------------
# Exchange mapping — which exchange each ticker trades on
# ---------------------------------------------------------------------------

TICKER_EXCHANGE: dict[str, Exchange] = {
    # NYSE
    "F": Exchange.NYSE,
    "BAC": Exchange.NYSE,
    "NIO": Exchange.NYSE,
    "T": Exchange.NYSE,
    "UBER": Exchange.NYSE,
    "XLF": Exchange.NYSE,
    "XLE": Exchange.NYSE,
    "XLK": Exchange.NYSE,
    "XLV": Exchange.NYSE,
    "SNAP": Exchange.NYSE,
    "SDS": Exchange.NYSE,
    # NASDAQ
    "AAL": Exchange.NASDAQ,
    "SOFI": Exchange.NASDAQ,
    "PLTR": Exchange.NASDAQ,
    "INTC": Exchange.NASDAQ,
    "AAPL": Exchange.NASDAQ,
    "MSFT": Exchange.NASDAQ,
    "NVDA": Exchange.NASDAQ,
    "GOOGL": Exchange.NASDAQ,
    "AMZN": Exchange.NASDAQ,
    "META": Exchange.NASDAQ,
    "SQQQ": Exchange.NASDAQ,
}


# ---------------------------------------------------------------------------
# Currency mapping — exchange -> trading currency
# ---------------------------------------------------------------------------

EXCHANGE_CURRENCY: dict[Exchange, Currency] = {
    Exchange.NYSE: Currency.USD,
    Exchange.NASDAQ: Currency.USD,
}


def ticker_currency(ticker: str) -> Currency:
    """Return the trading currency for a given ticker."""
    return Currency.USD


def ticker_market(ticker: str) -> Market:
    """Return the logical market for a given ticker."""
    return Market.US


# ---------------------------------------------------------------------------
# Schema version — must match the DB migration target
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 8

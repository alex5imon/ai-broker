"""SQLite database setup — table creation and schema verification."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from trading_bot.constants import SCHEMA_VERSION

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Full schema DDL — copied verbatim from SPEC.md Section 11
# ---------------------------------------------------------------------------

_SCHEMA_SQL: str = """
-- Trades table: completed trade records
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    side            TEXT NOT NULL,
    entry_time      TEXT NOT NULL,
    entry_price     REAL NOT NULL,
    quantity        INTEGER NOT NULL,
    exit_time       TEXT,
    exit_price      REAL,
    exit_reason     TEXT,
    gross_pnl       REAL,
    net_pnl         REAL,
    pnl_usd         REAL,
    signal_price    REAL,
    slippage_bps    REAL,
    sentiment_score REAL,
    signals         TEXT,
    hold_type       TEXT NOT NULL,
    phase           INTEGER NOT NULL,
    strategy_id     TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
CREATE INDEX IF NOT EXISTS idx_trades_exit_reason ON trades(exit_reason);
CREATE INDEX IF NOT EXISTS idx_trades_phase ON trades(phase);

-- Positions table: currently open positions and their management state
CREATE TABLE IF NOT EXISTS positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    currency        TEXT NOT NULL,
    sector          TEXT,
    quantity        INTEGER NOT NULL,
    entry_price     REAL NOT NULL,
    entry_time      TEXT NOT NULL,
    status          TEXT NOT NULL,
    stop_price      REAL,
    target_price    REAL,
    trailing_active INTEGER NOT NULL DEFAULT 0,
    trailing_distance REAL,
    hold_type       TEXT NOT NULL,
    phase           INTEGER NOT NULL,
    alpaca_order_id TEXT,
    alpaca_stop_order_id TEXT,
    alpaca_target_order_id TEXT,
    alpaca_trail_order_id TEXT,
    -- Strategy-driven exit (place_exit / place_limit_exit). Populated
    -- when the position transitions to CLOSING; cleared on FILLED or
    -- on canceled/expired/rejected rollback. Persisted so the next
    -- stateless tick can rehydrate the pending exit instead of
    -- re-evaluating and double-submitting. Added in V11.
    alpaca_exit_order_id TEXT,
    highest_price   REAL,
    strategy_id     TEXT NOT NULL,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);

-- Daily summaries: one row per trading day
CREATE TABLE IF NOT EXISTS daily_summaries (
    date            TEXT PRIMARY KEY,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    gross_pnl_usd   REAL NOT NULL DEFAULT 0.0,
    commissions_usd REAL NOT NULL DEFAULT 0.0,
    net_pnl_usd      REAL NOT NULL DEFAULT 0.0,
    account_equity_usd REAL NOT NULL,
    max_drawdown_pct REAL,
    win_rate        REAL,
    avg_win_usd     REAL,
    avg_loss_usd    REAL,
    profit_factor   REAL,
    phase           INTEGER NOT NULL,
    us_trades       INTEGER NOT NULL DEFAULT 0,
    notes           TEXT
);

-- Sentiment cache: avoid redundant API calls
CREATE TABLE IF NOT EXISTS sentiment_cache (
    ticker          TEXT NOT NULL,
    score           REAL NOT NULL,
    raw_score       REAL,
    source          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    PRIMARY KEY (ticker, source)
);

-- Earnings calendar: blackout management
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker          TEXT NOT NULL,
    earnings_date   TEXT NOT NULL,
    earnings_hour   TEXT,
    fetched_at      TEXT NOT NULL,
    PRIMARY KEY (ticker, earnings_date)
);

CREATE INDEX IF NOT EXISTS idx_earnings_date ON earnings_calendar(earnings_date);

-- Cooldowns: prevent rapid re-entry after exit
CREATE TABLE IF NOT EXISTS cooldowns (
    ticker          TEXT PRIMARY KEY,
    cooldown_until  TEXT NOT NULL
);

-- Config snapshots: track parameter changes over time
CREATE TABLE IF NOT EXISTS config_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    config_json     TEXT NOT NULL,
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Order rejections: track IB order failures for analysis
CREATE TABLE IF NOT EXISTS order_rejections (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    intended_price  REAL,
    intended_qty    INTEGER,
    reason          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    resolved        INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rejections_timestamp ON order_rejections(timestamp);

-- Phase transitions: audit trail for phase changes
CREATE TABLE IF NOT EXISTS phase_transitions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    from_phase      INTEGER NOT NULL,
    to_phase        INTEGER NOT NULL,
    direction       TEXT NOT NULL,
    account_equity_usd REAL NOT NULL,
    metrics_json    TEXT NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Backtest results: separate from live trades
CREATE TABLE IF NOT EXISTS backtest_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id     TEXT NOT NULL,
    run_date        TEXT NOT NULL,
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    initial_equity  REAL NOT NULL,
    final_equity    REAL NOT NULL,
    total_trades    INTEGER NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    gross_pnl       REAL NOT NULL,
    commissions     REAL NOT NULL,
    net_pnl         REAL NOT NULL,
    max_drawdown_pct REAL NOT NULL,
    sharpe_ratio    REAL,
    win_rate        REAL NOT NULL,
    profit_factor   REAL,
    avg_hold_minutes REAL,
    slippage_model  TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    trades_json     TEXT NOT NULL,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_backtest_run_date ON backtest_results(run_date);

-- Phase 0 assessments: portfolio cleanup scoring and dry-run records
CREATE TABLE IF NOT EXISTS phase0_assessments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    current_value_usd REAL,
    unrealized_pnl_usd REAL,
    score           INTEGER NOT NULL,
    classification  TEXT NOT NULL,
    scores_breakdown TEXT,
    reasoning       TEXT,
    recommended_action TEXT,
    trailing_stop_price REAL,
    dry_run         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_phase0_run_date ON phase0_assessments(run_date);

-- Strategy portfolios: virtual sub-portfolio state per strategy
CREATE TABLE IF NOT EXISTS strategy_portfolios (
    strategy_id     TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    initial_cash    REAL NOT NULL,
    current_cash    REAL NOT NULL,
    total_pnl       REAL NOT NULL DEFAULT 0.0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy_id);
CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_id);

-- Tick model state: per-strategy state carried across stateless GHA cron runs.
-- Replaces in-memory coordinator state that previously lived only in the
-- long-running asyncio loop.  state_json is a free-form blob so new fields
-- can be added without further migrations.
CREATE TABLE IF NOT EXISTS tick_state (
    strategy_id     TEXT PRIMARY KEY,
    last_bar_ts     TEXT,
    last_run_at     TEXT NOT NULL DEFAULT (datetime('now')),
    state_json      TEXT NOT NULL DEFAULT '{}',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Risk circuit breaker state: persists drawdown limits, consecutive-loss
-- counters, kill switches, etc. across runs.  The key is either 'global'
-- for account-wide circuits or a strategy_id for per-strategy circuits.
CREATE TABLE IF NOT EXISTS risk_circuit_state (
    key             TEXT PRIMARY KEY,
    tripped         INTEGER NOT NULL DEFAULT 0,
    tripped_at      TEXT,
    reason          TEXT,
    state_json      TEXT NOT NULL DEFAULT '{}',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version         INTEGER PRIMARY KEY,
    applied_at      TEXT NOT NULL DEFAULT (datetime('now')),
    description     TEXT
);
"""

_SEED_VERSION_SQL: str = (
    "INSERT OR IGNORE INTO schema_version (version, description) "
    "VALUES (?, 'V10 schema - USD-only column rename');"
)

# Expected tables — used for quick health check
_EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "trades",
        "positions",
        "daily_summaries",
        "sentiment_cache",
        "earnings_calendar",
        "cooldowns",
        "config_snapshots",
        "order_rejections",
        "phase_transitions",
        "phase0_assessments",
        "backtest_results",
        "strategy_portfolios",
        "tick_state",
        "risk_circuit_state",
        "schema_version",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_tables(db_path: str) -> None:
    """Create all tables in *db_path*, idempotently.

    The parent directory is created if it does not exist.  After running the
    DDL, the initial schema version row is inserted (``INSERT OR IGNORE``).
    """
    path: Path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn: sqlite3.Connection = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA_SQL)
        conn.execute(_SEED_VERSION_SQL, (SCHEMA_VERSION,))
        conn.commit()
        logger.info("Database tables created/verified at %s", path)
    finally:
        conn.close()


def get_schema_version(conn: sqlite3.Connection) -> int:
    """Return the highest schema version recorded, or ``0`` if the table is missing."""
    try:
        row = conn.execute(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        # Table does not exist yet
        pass
    return 0


def check_schema(conn: sqlite3.Connection) -> bool:
    """Return ``True`` if the database has the expected version and all tables.

    This is a lightweight pre-flight check — it does *not* validate column
    definitions, only table presence and version number.
    """
    version: int = get_schema_version(conn)
    if version != SCHEMA_VERSION:
        logger.warning(
            "Schema version mismatch: expected %d, got %d",
            SCHEMA_VERSION,
            version,
        )
        return False

    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    existing: set[str] = {r[0] for r in rows}

    missing: frozenset[str] = _EXPECTED_TABLES - existing
    if missing:
        logger.warning("Missing tables: %s", ", ".join(sorted(missing)))
        return False

    return True

"""Simple sequential migration system for the trading bot database.

For V4 this consists of a single migration (initial schema creation).  Future
versions add entries to ``_MIGRATIONS`` and ``run_migrations`` applies them in
order, skipping any that have already been applied.
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable

from trading_bot.constants import SCHEMA_VERSION
from trading_bot.db.schema import _SCHEMA_SQL, _SEED_VERSION_SQL, check_schema

logger: logging.Logger = logging.getLogger(__name__)

# Type alias for a migration function
MigrationFn = Callable[[sqlite3.Connection], None]


# ---------------------------------------------------------------------------
# Migration definitions
# ---------------------------------------------------------------------------

def _migration_v4(conn: sqlite3.Connection) -> None:
    """V4: initial schema — create all tables from scratch."""
    conn.executescript(_SCHEMA_SQL)
    conn.execute(_SEED_VERSION_SQL, (4,))
    logger.info("Applied migration V4: initial schema creation")


def _migration_v5(conn: sqlite3.Connection) -> None:
    """V5: multi-strategy support — add strategy_id, clean up IB columns.

    Idempotent: V4 on fresh installs already creates the full V5 schema
    (``trading_bot.db.schema._SCHEMA_SQL`` is the current shape), so the
    ALTERs and CREATEs below are guarded to skip already-applied changes.
    """

    # All identifier-name interpolations below come from hardcoded tuples,
    # NEVER from user input.  We keep the f-string form (sqlite parameters
    # cannot bind identifiers) but make the identifier set explicit so
    # this code is harder to misuse by future contributors.
    _ALLOWED_TABLES: frozenset[str] = frozenset({"trades", "positions", "settlements"})
    _RENAME_PAIRS: tuple[tuple[str, str], ...] = (
        ("ib_order_id", "alpaca_order_id"),
        ("ib_stop_order_id", "alpaca_stop_order_id"),
        ("ib_target_order_id", "alpaca_target_order_id"),
        ("ib_trail_order_id", "alpaca_trail_order_id"),
    )

    def _has_table(table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None

    def _has_column(table: str, column: str) -> bool:
        if table not in _ALLOWED_TABLES:
            raise ValueError(f"Unexpected table name: {table!r}")
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(r[1] == column for r in rows)

    # Add strategy_id columns if they don't exist. settlements is gated on
    # table existence: V9 drops the table on existing databases, and fresh
    # installs (post-V9) skip its creation entirely.
    for table in ("trades", "positions", "settlements"):
        if table == "settlements" and not _has_table(table):
            continue
        if not _has_column(table, "strategy_id"):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN strategy_id TEXT")

    # Rename IB columns to Alpaca equivalents (skip when already renamed).
    # ``old``/``new`` come from the hardcoded ``_RENAME_PAIRS`` tuple above.
    for old, new in _RENAME_PAIRS:
        if _has_column("positions", old) and not _has_column("positions", new):
            conn.execute(f"ALTER TABLE positions RENAME COLUMN {old} TO {new}")

    # Strategy portfolios table + indexes (create if missing)
    conn.executescript(
        """
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
        """
    )
    if _has_table("settlements"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_settlements_strategy "
            "ON settlements(strategy_id)"
        )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, description) "
        "VALUES (5, 'V5 schema - multi-strategy Alpaca trading bot')"
    )
    logger.info("Applied migration V5: multi-strategy support")


def _migration_v6(conn: sqlite3.Connection) -> None:
    """V6: make positions.strategy_id NOT NULL.

    Backfills any NULL strategy_id rows with 'unknown', then rebuilds the
    positions table with a NOT NULL constraint on strategy_id (SQLite does
    not support ALTER COLUMN, so we use the rename-create-copy-drop pattern).
    """

    # Short-circuit on fresh installs: if _migration_v4 already created the
    # table with strategy_id NOT NULL, nothing to do.
    rows = conn.execute("PRAGMA table_info(positions)").fetchall()
    strategy_col = next((r for r in rows if r[1] == "strategy_id"), None)
    if strategy_col is not None and strategy_col[3] == 1:
        # notnull flag already set
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, description) "
            "VALUES (6, 'V6 schema - positions.strategy_id NOT NULL')"
        )
        logger.info("V6: positions.strategy_id already NOT NULL, noop")
        return

    # Backfill NULLs
    conn.execute(
        "UPDATE positions SET strategy_id = 'unknown' WHERE strategy_id IS NULL"
    )

    # Rename-create-copy-drop
    conn.executescript(
        """
        ALTER TABLE positions RENAME TO positions_old;

        CREATE TABLE positions (
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
            highest_price   REAL,
            strategy_id     TEXT NOT NULL,
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        INSERT INTO positions (
            id, ticker, exchange, currency, sector, quantity, entry_price,
            entry_time, status, stop_price, target_price, trailing_active,
            trailing_distance, hold_type, phase, alpaca_order_id,
            alpaca_stop_order_id, alpaca_target_order_id, alpaca_trail_order_id,
            highest_price, strategy_id, updated_at
        )
        SELECT
            id, ticker, exchange, currency, sector, quantity, entry_price,
            entry_time, status, stop_price, target_price, trailing_active,
            trailing_distance, hold_type, phase, alpaca_order_id,
            alpaca_stop_order_id, alpaca_target_order_id, alpaca_trail_order_id,
            highest_price, strategy_id, updated_at
        FROM positions_old;

        DROP TABLE positions_old;

        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
        CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
        CREATE INDEX IF NOT EXISTS idx_positions_strategy ON positions(strategy_id);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, description) "
        "VALUES (6, 'V6 schema - positions.strategy_id NOT NULL')"
    )
    logger.info("Applied migration V6: positions.strategy_id NOT NULL")


def _migration_v7(conn: sqlite3.Connection) -> None:
    """V7: tick model persistence — tick_state + risk_circuit_state tables.

    These tables persist state that previously lived only in the long-running
    asyncio process, so that the stateless GHA cron tick can resume from the
    last known state each run.  Idempotent.
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tick_state (
            strategy_id     TEXT PRIMARY KEY,
            last_bar_ts     TEXT,
            last_run_at     TEXT NOT NULL DEFAULT (datetime('now')),
            state_json      TEXT NOT NULL DEFAULT '{}',
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS risk_circuit_state (
            key             TEXT PRIMARY KEY,
            tripped         INTEGER NOT NULL DEFAULT 0,
            tripped_at      TEXT,
            reason          TEXT,
            state_json      TEXT NOT NULL DEFAULT '{}',
            updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, description) "
        "VALUES (7, 'V7 schema - tick_state + risk_circuit_state')"
    )
    logger.info("Applied migration V7: tick_state + risk_circuit_state")


def _migration_v8(conn: sqlite3.Connection) -> None:
    """V8: retro-convert phantom-CLOSED positions to ENTRY_FAILED.

    Pre-V8, every path in ``order_manager`` that aborted an entry stamped
    the row ``status='CLOSED'`` even though no fill ever existed on Alpaca
    (entry-timeout, submit error, rejection-after-DB-insert).  These rows
    pollute every "completed trade" report and exit-recovery scan because
    they look identical to a real round-trip.

    A row is a phantom close iff ALL of:
      - ``status = 'CLOSED'``
      - ``alpaca_order_id IS NULL``
        (entry order id is only set on a successful submit_order)
      - the paired ``trades`` row has ``exit_time IS NULL``
        (the close path never wrote exit data — confirms no real fill)

    Rows where ``alpaca_order_id`` IS set but the entry was canceled at the
    broker are NOT touched here — they require a live Alpaca lookup to
    classify, which is the Phase 1 reconcile tool's job.  This migration
    only flips the obvious cases that can be detected from local DB state
    alone.

    The migration is reversible by replaying the SELECT that produced the
    target ids and restoring ``status='CLOSED'``; it never deletes rows
    or columns.  Operationally it is also reversible by restoring the
    SQLite file from the GHA cache artifact.
    """
    target_ids: list[int] = [
        row[0]
        for row in conn.execute(
            """
            SELECT p.id
              FROM positions p
              LEFT JOIN trades t
                ON t.ticker = p.ticker
               AND t.entry_time = p.entry_time
             WHERE p.status = 'CLOSED'
               AND p.alpaca_order_id IS NULL
               AND (t.exit_time IS NULL OR t.id IS NULL)
            """
        ).fetchall()
    ]

    if target_ids:
        # IN (?,?,?…) with positional binds — no string interpolation of
        # row data.
        placeholders: str = ",".join("?" for _ in target_ids)
        conn.execute(
            f"UPDATE positions SET status = 'ENTRY_FAILED', "
            f"updated_at = datetime('now') "
            f"WHERE id IN ({placeholders})",
            target_ids,
        )
    logger.info(
        "V8: retro-converted %d phantom-CLOSED row(s) to ENTRY_FAILED",
        len(target_ids),
    )

    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, description) "
        "VALUES (8, 'V8 schema - ENTRY_FAILED terminal status (data-only)')"
    )
    logger.info("Applied migration V8: ENTRY_FAILED terminal status")


def _migration_v9(conn: sqlite3.Connection) -> None:
    """V9: drop the unused ``settlements`` table and its indexes.

    The settlement-tracking subsystem was instantiated but never wired:
    ``SettlementTracker.record_sale`` had zero production callers, so the
    table was always empty and every reader returned 0. Removing it
    eliminates dead schema, dead code paths, and the misleading
    "T+1 settlement tracking" claim previously documented in CLAUDE.md.
    """
    conn.execute("DROP INDEX IF EXISTS idx_settlements_settled")
    conn.execute("DROP INDEX IF EXISTS idx_settlements_settle_date")
    conn.execute("DROP INDEX IF EXISTS idx_settlements_strategy")
    conn.execute("DROP TABLE IF EXISTS settlements")
    conn.execute(
        "INSERT OR REPLACE INTO schema_version (version, description) "
        "VALUES (9, 'V9 schema - settlements table removed')"
    )
    logger.info("Applied migration V9: dropped settlements table")


_MIGRATIONS: list[tuple[int, str, MigrationFn]] = [
    (4, "V4 schema - multi-market adaptive trading bot", _migration_v4),
    (5, "V5 schema - multi-strategy Alpaca trading bot", _migration_v5),
    (6, "V6 schema - positions.strategy_id NOT NULL", _migration_v6),
    (7, "V7 schema - tick_state + risk_circuit_state", _migration_v7),
    (8, "V8 schema - ENTRY_FAILED terminal status (data-only)", _migration_v8),
    (9, "V9 schema - settlements table removed", _migration_v9),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_migrations(db_path: str) -> None:
    """Open (or create) the database at *db_path* and apply any pending migrations.

    Migrations are applied inside individual transactions so that a failure in
    one migration does not corrupt previously-applied migrations.  The
    ``schema_version`` table is used to track which versions have been applied.
    """
    path: Path = Path(db_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    conn: sqlite3.Connection = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    try:
        current_version: int = _get_current_version(conn)
        logger.info(
            "Database at %s — current schema version: %d, target: %d",
            path,
            current_version,
            SCHEMA_VERSION,
        )

        applied: int = 0
        for version, description, fn in _MIGRATIONS:
            if version <= current_version:
                continue
            logger.info("Applying migration V%d: %s", version, description)
            fn(conn)
            conn.commit()
            applied += 1

        if applied == 0:
            logger.info("Database schema is up to date (V%d)", current_version)
        else:
            logger.info("Applied %d migration(s); now at V%d", applied, SCHEMA_VERSION)

        # Final sanity check
        if not check_schema(conn):
            logger.error("Post-migration schema check failed")
            raise RuntimeError("Schema check failed after migrations")

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or ``0`` if none."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row and row[0] is not None:
            return int(row[0])
    except sqlite3.OperationalError:
        # schema_version table does not exist yet — version 0
        pass
    return 0

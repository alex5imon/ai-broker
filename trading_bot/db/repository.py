"""Data access layer — the ONLY module that writes SQL.

Every function accepts a ``sqlite3.Connection`` (with ``row_factory`` set to
``sqlite3.Row``) and returns plain dicts so that callers are decoupled from
the database schema.  All queries use parameterised placeholders.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from trading_bot.constants import TZ_EASTERN

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Convert a ``sqlite3.Row`` to a plain dict, or ``None`` if *row* is ``None``."""
    if row is None:
        return None
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert a list of ``sqlite3.Row`` objects to a list of dicts."""
    return [dict(r) for r in rows]


def _ensure_row_factory(conn: sqlite3.Connection) -> None:
    """Set ``row_factory`` to ``sqlite3.Row`` if not already set."""
    if conn.row_factory is not sqlite3.Row:
        conn.row_factory = sqlite3.Row


def _now_eastern_iso() -> str:
    """Return the current time in US/Eastern as an ISO-8601 string."""
    return datetime.now(TZ_EASTERN).isoformat()


def _today_eastern() -> str:
    """Return today's date in US/Eastern as ``YYYY-MM-DD``."""
    return datetime.now(TZ_EASTERN).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------

def save_trade(conn: sqlite3.Connection, trade: dict[str, Any]) -> int:
    """Insert a new trade record and return its ``id``.

    *trade* must contain at least the NOT NULL columns of the ``trades`` table.
    """
    _ensure_row_factory(conn)
    cur: sqlite3.Cursor = conn.execute(
        """
        INSERT INTO trades (
            ticker, exchange, currency, side, entry_time, entry_price,
            quantity, exit_time, exit_price, exit_reason,
            gross_pnl, commission, net_pnl, pnl_gbp, fx_rate,
            signal_price, slippage_bps, sentiment_score, signals,
            hold_type, phase, notes
        ) VALUES (
            :ticker, :exchange, :currency, :side, :entry_time, :entry_price,
            :quantity, :exit_time, :exit_price, :exit_reason,
            :gross_pnl, :commission, :net_pnl, :pnl_gbp, :fx_rate,
            :signal_price, :slippage_bps, :sentiment_score, :signals,
            :hold_type, :phase, :notes
        )
        """,
        {
            "ticker": trade["ticker"],
            "exchange": trade["exchange"],
            "currency": trade["currency"],
            "side": trade["side"],
            "entry_time": trade["entry_time"],
            "entry_price": trade["entry_price"],
            "quantity": trade["quantity"],
            "exit_time": trade.get("exit_time"),
            "exit_price": trade.get("exit_price"),
            "exit_reason": trade.get("exit_reason"),
            "gross_pnl": trade.get("gross_pnl"),
            "commission": trade.get("commission"),
            "net_pnl": trade.get("net_pnl"),
            "pnl_gbp": trade.get("pnl_gbp"),
            "fx_rate": trade.get("fx_rate"),
            "signal_price": trade.get("signal_price"),
            "slippage_bps": trade.get("slippage_bps"),
            "sentiment_score": trade.get("sentiment_score"),
            "signals": trade.get("signals"),
            "hold_type": trade["hold_type"],
            "phase": trade["phase"],
            "notes": trade.get("notes"),
        },
    )
    conn.commit()
    trade_id: int = cur.lastrowid  # type: ignore[assignment]
    logger.debug("Saved trade id=%d ticker=%s", trade_id, trade["ticker"])
    return trade_id


def update_trade_exit(conn: sqlite3.Connection, trade_id: int, exit_data: dict[str, Any]) -> None:
    """Update an existing trade with exit information."""
    _ensure_row_factory(conn)
    conn.execute(
        """
        UPDATE trades SET
            exit_time       = :exit_time,
            exit_price      = :exit_price,
            exit_reason     = :exit_reason,
            gross_pnl       = :gross_pnl,
            commission      = :commission,
            net_pnl         = :net_pnl,
            pnl_gbp         = :pnl_gbp,
            fx_rate         = :fx_rate,
            slippage_bps    = :slippage_bps
        WHERE id = :id
        """,
        {
            "id": trade_id,
            "exit_time": exit_data["exit_time"],
            "exit_price": exit_data["exit_price"],
            "exit_reason": exit_data["exit_reason"],
            "gross_pnl": exit_data.get("gross_pnl"),
            "commission": exit_data.get("commission"),
            "net_pnl": exit_data.get("net_pnl"),
            "pnl_gbp": exit_data.get("pnl_gbp"),
            "fx_rate": exit_data.get("fx_rate"),
            "slippage_bps": exit_data.get("slippage_bps"),
        },
    )
    conn.commit()
    logger.debug("Updated trade id=%d exit_reason=%s", trade_id, exit_data["exit_reason"])


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def get_open_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all positions that are not CLOSED."""
    _ensure_row_factory(conn)
    rows = conn.execute(
        "SELECT * FROM positions WHERE status != 'CLOSED' ORDER BY entry_time"
    ).fetchall()
    return _rows_to_dicts(rows)


def save_position(conn: sqlite3.Connection, position: dict[str, Any]) -> int:
    """Insert a new position record and return its ``id``."""
    _ensure_row_factory(conn)
    cur: sqlite3.Cursor = conn.execute(
        """
        INSERT INTO positions (
            ticker, exchange, currency, sector, quantity, entry_price,
            entry_time, status, stop_price, target_price,
            trailing_active, trailing_distance, hold_type, phase,
            alpaca_order_id, alpaca_stop_order_id, alpaca_target_order_id,
            alpaca_trail_order_id, oca_group, highest_price
        ) VALUES (
            :ticker, :exchange, :currency, :sector, :quantity, :entry_price,
            :entry_time, :status, :stop_price, :target_price,
            :trailing_active, :trailing_distance, :hold_type, :phase,
            :alpaca_order_id, :alpaca_stop_order_id, :alpaca_target_order_id,
            :alpaca_trail_order_id, :oca_group, :highest_price
        )
        """,
        {
            "ticker": position["ticker"],
            "exchange": position["exchange"],
            "currency": position["currency"],
            "sector": position.get("sector"),
            "quantity": position["quantity"],
            "entry_price": position["entry_price"],
            "entry_time": position["entry_time"],
            "status": position["status"],
            "stop_price": position.get("stop_price"),
            "target_price": position.get("target_price"),
            "trailing_active": position.get("trailing_active", 0),
            "trailing_distance": position.get("trailing_distance"),
            "hold_type": position["hold_type"],
            "phase": position["phase"],
            "alpaca_order_id": position.get("alpaca_order_id"),
            "alpaca_stop_order_id": position.get("alpaca_stop_order_id"),
            "alpaca_target_order_id": position.get("alpaca_target_order_id"),
            "alpaca_trail_order_id": position.get("alpaca_trail_order_id"),
            "oca_group": position.get("oca_group"),
            "highest_price": position.get("highest_price"),
        },
    )
    conn.commit()
    pos_id: int = cur.lastrowid  # type: ignore[assignment]
    logger.debug("Saved position id=%d ticker=%s", pos_id, position["ticker"])
    return pos_id


def update_position(conn: sqlite3.Connection, position_id: int, updates: dict[str, Any]) -> None:
    """Update specific fields on an existing position.

    *updates* is a dict of column-name -> value.  Only columns present in
    *updates* are modified; ``updated_at`` is always refreshed.
    """
    _ensure_row_factory(conn)
    if not updates:
        return

    # Build SET clause dynamically from the supplied keys
    allowed_columns: set[str] = {
        "status", "stop_price", "target_price", "trailing_active",
        "trailing_distance", "alpaca_order_id", "alpaca_stop_order_id",
        "alpaca_target_order_id", "alpaca_trail_order_id", "oca_group",
        "highest_price", "quantity",
    }
    set_parts: list[str] = []
    params: dict[str, Any] = {"_id": position_id}
    for key, value in updates.items():
        if key not in allowed_columns:
            logger.warning("Ignoring unknown position column: %s", key)
            continue
        set_parts.append(f"{key} = :{key}")
        params[key] = value

    if not set_parts:
        return

    set_parts.append("updated_at = datetime('now')")
    sql: str = f"UPDATE positions SET {', '.join(set_parts)} WHERE id = :_id"
    conn.execute(sql, params)
    conn.commit()
    logger.debug("Updated position id=%d fields=%s", position_id, list(updates.keys()))


# ---------------------------------------------------------------------------
# Settlements
# ---------------------------------------------------------------------------

def save_settlement(conn: sqlite3.Connection, settlement: dict[str, Any]) -> int:
    """Insert a settlement record and return its ``id``."""
    _ensure_row_factory(conn)
    cur: sqlite3.Cursor = conn.execute(
        """
        INSERT INTO settlements (
            trade_id, ticker, amount, currency, amount_gbp,
            sell_date, settle_date, settled
        ) VALUES (
            :trade_id, :ticker, :amount, :currency, :amount_gbp,
            :sell_date, :settle_date, :settled
        )
        """,
        {
            "trade_id": settlement.get("trade_id"),
            "ticker": settlement["ticker"],
            "amount": settlement["amount"],
            "currency": settlement["currency"],
            "amount_gbp": settlement["amount_gbp"],
            "sell_date": settlement["sell_date"],
            "settle_date": settlement["settle_date"],
            "settled": settlement.get("settled", 0),
        },
    )
    conn.commit()
    settle_id: int = cur.lastrowid  # type: ignore[assignment]
    logger.debug("Saved settlement id=%d ticker=%s", settle_id, settlement["ticker"])
    return settle_id


def get_pending_settlements(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all settlements where ``settled = 0``."""
    _ensure_row_factory(conn)
    rows = conn.execute(
        "SELECT * FROM settlements WHERE settled = 0 ORDER BY settle_date"
    ).fetchall()
    return _rows_to_dicts(rows)


def mark_settlement_complete(conn: sqlite3.Connection, settlement_id: int) -> None:
    """Mark a settlement as complete (``settled = 1``)."""
    conn.execute(
        "UPDATE settlements SET settled = 1 WHERE id = ?", (settlement_id,)
    )
    conn.commit()
    logger.debug("Settlement id=%d marked complete", settlement_id)


def get_settled_cash_gbp(conn: sqlite3.Connection, as_of_date: str) -> float:
    """Return the total GBP value of settlements that have settled on or before *as_of_date*.

    *as_of_date* should be ``YYYY-MM-DD`` format.
    """
    _ensure_row_factory(conn)
    row = conn.execute(
        """
        SELECT COALESCE(SUM(amount_gbp), 0.0) AS total
        FROM settlements
        WHERE settle_date <= ? AND settled = 1
        """,
        (as_of_date,),
    ).fetchone()
    return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Sentiment
# ---------------------------------------------------------------------------

def save_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    score: float,
    source: str,
    raw_score: float | None = None,
) -> None:
    """Upsert a sentiment cache entry (INSERT OR REPLACE on primary key)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO sentiment_cache
            (ticker, score, raw_score, source, timestamp)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ticker, score, raw_score, source, _now_eastern_iso()),
    )
    conn.commit()
    logger.debug("Saved sentiment ticker=%s score=%.3f source=%s", ticker, score, source)


def get_sentiment(
    conn: sqlite3.Connection,
    ticker: str,
    max_age_minutes: int = 30,
) -> float | None:
    """Return the cached sentiment score for *ticker* if fresh enough, else ``None``.

    Freshness is determined by comparing the ``timestamp`` column against
    ``max_age_minutes``.
    """
    _ensure_row_factory(conn)
    cutoff: str = (
        datetime.now(TZ_EASTERN) - timedelta(minutes=max_age_minutes)
    ).isoformat()
    row = conn.execute(
        """
        SELECT score FROM sentiment_cache
        WHERE ticker = ? AND timestamp >= ?
        ORDER BY timestamp DESC LIMIT 1
        """,
        (ticker, cutoff),
    ).fetchone()
    if row is not None:
        return float(row["score"])
    return None


# ---------------------------------------------------------------------------
# Earnings calendar
# ---------------------------------------------------------------------------

def save_earnings(
    conn: sqlite3.Connection,
    ticker: str,
    date: str,
    hour: str | None = None,
) -> None:
    """Upsert an earnings calendar entry."""
    conn.execute(
        """
        INSERT OR REPLACE INTO earnings_calendar
            (ticker, earnings_date, earnings_hour, fetched_at)
        VALUES (?, ?, ?, ?)
        """,
        (ticker, date, hour, _now_eastern_iso()),
    )
    conn.commit()
    logger.debug("Saved earnings ticker=%s date=%s hour=%s", ticker, date, hour)


def is_in_earnings_blackout(
    conn: sqlite3.Connection,
    ticker: str,
    blackout_hours: int = 48,
) -> bool:
    """Return ``True`` if *ticker* has an earnings date within *blackout_hours*."""
    _ensure_row_factory(conn)
    now: datetime = datetime.now(TZ_EASTERN)
    window_start: str = (now - timedelta(hours=blackout_hours)).strftime("%Y-%m-%d")
    window_end: str = (now + timedelta(hours=blackout_hours)).strftime("%Y-%m-%d")

    row = conn.execute(
        """
        SELECT 1 FROM earnings_calendar
        WHERE ticker = ? AND earnings_date BETWEEN ? AND ?
        LIMIT 1
        """,
        (ticker, window_start, window_end),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Cooldowns
# ---------------------------------------------------------------------------

def get_cooldown(conn: sqlite3.Connection, ticker: str) -> datetime | None:
    """Return the cooldown expiry for *ticker*, or ``None`` if not under cooldown."""
    _ensure_row_factory(conn)
    row = conn.execute(
        "SELECT cooldown_until FROM cooldowns WHERE ticker = ?", (ticker,)
    ).fetchone()
    if row is None:
        return None
    return datetime.fromisoformat(row["cooldown_until"])


def set_cooldown(conn: sqlite3.Connection, ticker: str, until: datetime) -> None:
    """Set (or replace) the cooldown for *ticker*."""
    conn.execute(
        "INSERT OR REPLACE INTO cooldowns (ticker, cooldown_until) VALUES (?, ?)",
        (ticker, until.isoformat()),
    )
    conn.commit()
    logger.debug("Set cooldown ticker=%s until=%s", ticker, until.isoformat())


def clear_expired_cooldowns(conn: sqlite3.Connection) -> None:
    """Delete all cooldowns whose expiry has passed."""
    now_iso: str = _now_eastern_iso()
    deleted: int = conn.execute(
        "DELETE FROM cooldowns WHERE cooldown_until <= ?", (now_iso,)
    ).rowcount
    conn.commit()
    if deleted:
        logger.debug("Cleared %d expired cooldown(s)", deleted)


# ---------------------------------------------------------------------------
# Daily summaries
# ---------------------------------------------------------------------------

def save_daily_summary(conn: sqlite3.Connection, summary: dict[str, Any]) -> None:
    """Insert or replace a daily summary row (keyed by ``date``)."""
    conn.execute(
        """
        INSERT OR REPLACE INTO daily_summaries (
            date, total_trades, wins, losses,
            gross_pnl_gbp, commissions_gbp, net_pnl_gbp,
            account_equity_gbp, max_drawdown_pct, win_rate,
            avg_win_gbp, avg_loss_gbp, profit_factor,
            phase, lse_trades, us_trades, commission_ratio, notes
        ) VALUES (
            :date, :total_trades, :wins, :losses,
            :gross_pnl_gbp, :commissions_gbp, :net_pnl_gbp,
            :account_equity_gbp, :max_drawdown_pct, :win_rate,
            :avg_win_gbp, :avg_loss_gbp, :profit_factor,
            :phase, :lse_trades, :us_trades, :commission_ratio, :notes
        )
        """,
        {
            "date": summary["date"],
            "total_trades": summary.get("total_trades", 0),
            "wins": summary.get("wins", 0),
            "losses": summary.get("losses", 0),
            "gross_pnl_gbp": summary.get("gross_pnl_gbp", 0.0),
            "commissions_gbp": summary.get("commissions_gbp", 0.0),
            "net_pnl_gbp": summary.get("net_pnl_gbp", 0.0),
            "account_equity_gbp": summary["account_equity_gbp"],
            "max_drawdown_pct": summary.get("max_drawdown_pct"),
            "win_rate": summary.get("win_rate"),
            "avg_win_gbp": summary.get("avg_win_gbp"),
            "avg_loss_gbp": summary.get("avg_loss_gbp"),
            "profit_factor": summary.get("profit_factor"),
            "phase": summary["phase"],
            "lse_trades": summary.get("lse_trades", 0),
            "us_trades": summary.get("us_trades", 0),
            "commission_ratio": summary.get("commission_ratio"),
            "notes": summary.get("notes"),
        },
    )
    conn.commit()
    logger.debug("Saved daily summary for %s", summary["date"])


def get_recent_daily_summaries(
    conn: sqlite3.Connection,
    n_days: int = 30,
) -> list[dict[str, Any]]:
    """Return the most recent *n_days* daily summaries, newest first."""
    _ensure_row_factory(conn)
    rows = conn.execute(
        "SELECT * FROM daily_summaries ORDER BY date DESC LIMIT ?",
        (n_days,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Order rejections
# ---------------------------------------------------------------------------

def save_order_rejection(conn: sqlite3.Connection, rejection: dict[str, Any]) -> None:
    """Insert an order rejection record."""
    conn.execute(
        """
        INSERT INTO order_rejections (
            ticker, exchange, order_type, intended_price,
            intended_qty, reason, timestamp, resolved
        ) VALUES (
            :ticker, :exchange, :order_type, :intended_price,
            :intended_qty, :reason, :timestamp, :resolved
        )
        """,
        {
            "ticker": rejection["ticker"],
            "exchange": rejection["exchange"],
            "order_type": rejection["order_type"],
            "intended_price": rejection.get("intended_price"),
            "intended_qty": rejection.get("intended_qty"),
            "reason": rejection["reason"],
            "timestamp": rejection.get("timestamp", _now_eastern_iso()),
            "resolved": rejection.get("resolved", 0),
        },
    )
    conn.commit()
    logger.debug(
        "Saved order rejection ticker=%s reason=%s",
        rejection["ticker"],
        rejection["reason"],
    )


def get_recent_rejections(
    conn: sqlite3.Connection,
    minutes: int = 10,
) -> list[dict[str, Any]]:
    """Return order rejections from the last *minutes* minutes."""
    _ensure_row_factory(conn)
    cutoff: str = (
        datetime.now(TZ_EASTERN) - timedelta(minutes=minutes)
    ).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM order_rejections
        WHERE timestamp >= ? AND resolved = 0
        ORDER BY timestamp DESC
        """,
        (cutoff,),
    ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Phase transitions
# ---------------------------------------------------------------------------

def save_phase_transition(conn: sqlite3.Connection, transition: dict[str, Any]) -> None:
    """Insert a phase transition audit record."""
    conn.execute(
        """
        INSERT INTO phase_transitions (
            date, from_phase, to_phase, direction,
            account_equity_gbp, metrics_json, reason
        ) VALUES (
            :date, :from_phase, :to_phase, :direction,
            :account_equity_gbp, :metrics_json, :reason
        )
        """,
        {
            "date": transition["date"],
            "from_phase": transition["from_phase"],
            "to_phase": transition["to_phase"],
            "direction": transition["direction"],
            "account_equity_gbp": transition["account_equity_gbp"],
            "metrics_json": transition["metrics_json"],
            "reason": transition["reason"],
        },
    )
    conn.commit()
    logger.info(
        "Phase transition: %d -> %d (%s) equity=%.2f",
        transition["from_phase"],
        transition["to_phase"],
        transition["direction"],
        transition["account_equity_gbp"],
    )


def get_phase_transitions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all phase transition records, newest first."""
    _ensure_row_factory(conn)
    rows = conn.execute(
        "SELECT * FROM phase_transitions ORDER BY created_at DESC"
    ).fetchall()
    return _rows_to_dicts(rows)


def is_phase0_complete(conn: sqlite3.Connection) -> bool:
    """Return ``True`` if there is a transition record FROM phase 0.

    This indicates the Phase-0 portfolio cleanup has been completed.
    """
    _ensure_row_factory(conn)
    row = conn.execute(
        "SELECT 1 FROM phase_transitions WHERE from_phase = 0 LIMIT 1"
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Phase 0 assessments
# ---------------------------------------------------------------------------

def save_phase0_assessments(
    conn: sqlite3.Connection,
    assessments: list[dict[str, Any]],
    run_date: str,
    dry_run: bool = True,
) -> None:
    """Save a batch of Phase 0 assessment records for a single run."""
    import json

    for a in assessments:
        conn.execute(
            """
            INSERT INTO phase0_assessments (
                run_date, ticker, exchange, current_value_gbp,
                unrealized_pnl_gbp, score, classification,
                scores_breakdown, reasoning, recommended_action,
                trailing_stop_price, dry_run
            ) VALUES (
                :run_date, :ticker, :exchange, :current_value_gbp,
                :unrealized_pnl_gbp, :score, :classification,
                :scores_breakdown, :reasoning, :recommended_action,
                :trailing_stop_price, :dry_run
            )
            """,
            {
                "run_date": run_date,
                "ticker": a["ticker"],
                "exchange": a["exchange"],
                "current_value_gbp": a.get("current_value_gbp", 0.0),
                "unrealized_pnl_gbp": a.get("unrealized_pnl_gbp", 0.0),
                "score": a["score"],
                "classification": a["classification"],
                "scores_breakdown": json.dumps(a.get("scores_breakdown", {})),
                "reasoning": a.get("reasoning", ""),
                "recommended_action": a.get("recommended_action", ""),
                "trailing_stop_price": a.get("trailing_stop_price"),
                "dry_run": 1 if dry_run else 0,
            },
        )
    conn.commit()
    logger.info(
        "Saved %d Phase 0 assessments for %s (dry_run=%s)",
        len(assessments),
        run_date,
        dry_run,
    )


def get_phase0_assessments(
    conn: sqlite3.Connection,
    run_date: str | None = None,
) -> list[dict[str, Any]]:
    """Return Phase 0 assessments, optionally filtered by run_date.

    Returns most recent run if no date given.
    """
    _ensure_row_factory(conn)
    if run_date:
        rows = conn.execute(
            "SELECT * FROM phase0_assessments WHERE run_date = ? ORDER BY score ASC",
            (run_date,),
        ).fetchall()
    else:
        # Get the most recent run_date
        latest = conn.execute(
            "SELECT run_date FROM phase0_assessments ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not latest:
            return []
        rows = conn.execute(
            "SELECT * FROM phase0_assessments WHERE run_date = ? ORDER BY score ASC",
            (latest["run_date"],),
        ).fetchall()
    return _rows_to_dicts(rows)


# ---------------------------------------------------------------------------
# Config snapshots
# ---------------------------------------------------------------------------

def save_config_snapshot(
    conn: sqlite3.Connection,
    config_json: str,
    notes: str = "",
) -> None:
    """Save a timestamped config snapshot."""
    conn.execute(
        """
        INSERT INTO config_snapshots (date, config_json, notes)
        VALUES (?, ?, ?)
        """,
        (_today_eastern(), config_json, notes),
    )
    conn.commit()
    logger.debug("Saved config snapshot")


# ---------------------------------------------------------------------------
# Aggregate queries
# ---------------------------------------------------------------------------

def get_trade_count_today(conn: sqlite3.Connection) -> int:
    """Return the number of trades opened today (US/Eastern date)."""
    _ensure_row_factory(conn)
    today: str = _today_eastern()
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM trades
        WHERE substr(entry_time, 1, 10) = ?
        """,
        (today,),
    ).fetchone()
    return int(row["cnt"]) if row else 0


def get_daily_pnl_gbp(conn: sqlite3.Connection) -> float:
    """Return today's net P&L in GBP (sum of ``pnl_gbp`` for closed trades today)."""
    _ensure_row_factory(conn)
    today: str = _today_eastern()
    row = conn.execute(
        """
        SELECT COALESCE(SUM(pnl_gbp), 0.0) AS total
        FROM trades
        WHERE substr(exit_time, 1, 10) = ? AND pnl_gbp IS NOT NULL
        """,
        (today,),
    ).fetchone()
    return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Backtest results
# ---------------------------------------------------------------------------

def save_backtest_result(conn: sqlite3.Connection, result: dict[str, Any]) -> int:
    """Insert a backtest result and return its ``id``."""
    _ensure_row_factory(conn)
    cur: sqlite3.Cursor = conn.execute(
        """
        INSERT INTO backtest_results (
            backtest_id, run_date, start_date, end_date,
            initial_equity, final_equity, total_trades,
            wins, losses, gross_pnl, commissions, net_pnl,
            max_drawdown_pct, sharpe_ratio, win_rate,
            profit_factor, avg_hold_minutes, slippage_model,
            parameters_json, trades_json, notes
        ) VALUES (
            :backtest_id, :run_date, :start_date, :end_date,
            :initial_equity, :final_equity, :total_trades,
            :wins, :losses, :gross_pnl, :commissions, :net_pnl,
            :max_drawdown_pct, :sharpe_ratio, :win_rate,
            :profit_factor, :avg_hold_minutes, :slippage_model,
            :parameters_json, :trades_json, :notes
        )
        """,
        {
            "backtest_id": result["backtest_id"],
            "run_date": result["run_date"],
            "start_date": result["start_date"],
            "end_date": result["end_date"],
            "initial_equity": result["initial_equity"],
            "final_equity": result["final_equity"],
            "total_trades": result["total_trades"],
            "wins": result["wins"],
            "losses": result["losses"],
            "gross_pnl": result["gross_pnl"],
            "commissions": result["commissions"],
            "net_pnl": result["net_pnl"],
            "max_drawdown_pct": result["max_drawdown_pct"],
            "sharpe_ratio": result.get("sharpe_ratio"),
            "win_rate": result["win_rate"],
            "profit_factor": result.get("profit_factor"),
            "avg_hold_minutes": result.get("avg_hold_minutes"),
            "slippage_model": result["slippage_model"],
            "parameters_json": result["parameters_json"],
            "trades_json": result["trades_json"],
            "notes": result.get("notes"),
        },
    )
    conn.commit()
    bt_id: int = cur.lastrowid  # type: ignore[assignment]
    logger.debug("Saved backtest result id=%d backtest_id=%s", bt_id, result["backtest_id"])
    return bt_id


# ---------------------------------------------------------------------------
# Tick state — per-strategy state carried across stateless cron runs
# ---------------------------------------------------------------------------

def save_tick_state(
    conn: sqlite3.Connection,
    strategy_id: str,
    *,
    last_bar_ts: str | None,
    state: dict[str, Any] | None = None,
) -> None:
    """Upsert the tick state for *strategy_id*.

    *last_bar_ts* is the ISO timestamp of the most recently processed bar; it
    may be ``None`` if no bar has been processed yet.  *state* is an arbitrary
    JSON-serialisable dict of additional fields.
    """
    state_json: str = json.dumps(state or {}, sort_keys=True)
    now: str = _now_eastern_iso()
    conn.execute(
        """
        INSERT INTO tick_state (strategy_id, last_bar_ts, last_run_at, state_json, updated_at)
        VALUES (:strategy_id, :last_bar_ts, :now, :state_json, :now)
        ON CONFLICT(strategy_id) DO UPDATE SET
            last_bar_ts = excluded.last_bar_ts,
            last_run_at = excluded.last_run_at,
            state_json  = excluded.state_json,
            updated_at  = excluded.updated_at
        """,
        {
            "strategy_id": strategy_id,
            "last_bar_ts": last_bar_ts,
            "now": now,
            "state_json": state_json,
        },
    )
    conn.commit()


def load_tick_state(
    conn: sqlite3.Connection, strategy_id: str
) -> dict[str, Any] | None:
    """Return the tick state for *strategy_id*, or ``None`` if no row exists.

    The returned dict includes ``strategy_id``, ``last_bar_ts``, ``last_run_at``,
    ``updated_at``, plus all keys from the stored ``state_json`` blob merged in
    under a ``state`` key for callers that prefer structured access.
    """
    _ensure_row_factory(conn)
    row = conn.execute(
        "SELECT strategy_id, last_bar_ts, last_run_at, state_json, updated_at "
        "FROM tick_state WHERE strategy_id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        return None
    result: dict[str, Any] = dict(row)
    try:
        result["state"] = json.loads(result.pop("state_json") or "{}")
    except json.JSONDecodeError:
        logger.warning("Corrupt state_json for strategy %s; returning empty", strategy_id)
        result["state"] = {}
    return result


# ---------------------------------------------------------------------------
# Risk circuit state — persisted kill switches / drawdown counters
# ---------------------------------------------------------------------------

def save_risk_state(
    conn: sqlite3.Connection,
    key: str,
    *,
    tripped: bool,
    reason: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Upsert the risk circuit state for *key* (``'global'`` or a strategy id).

    When *tripped* flips from False to True, ``tripped_at`` is set to *now*.
    When it flips back to False, ``tripped_at`` and ``reason`` are cleared.
    """
    _ensure_row_factory(conn)
    prior = conn.execute(
        "SELECT tripped FROM risk_circuit_state WHERE key = ?",
        (key,),
    ).fetchone()
    now: str = _now_eastern_iso()
    state_json: str = json.dumps(state or {}, sort_keys=True)

    if tripped:
        # Preserve the original tripped_at on an update; only stamp it on first trip.
        tripped_at: str | None = now
        if prior is not None and int(prior["tripped"]) == 1:
            existing = conn.execute(
                "SELECT tripped_at FROM risk_circuit_state WHERE key = ?",
                (key,),
            ).fetchone()
            if existing and existing["tripped_at"]:
                tripped_at = existing["tripped_at"]
    else:
        tripped_at = None
        reason = None

    conn.execute(
        """
        INSERT INTO risk_circuit_state (key, tripped, tripped_at, reason, state_json, updated_at)
        VALUES (:key, :tripped, :tripped_at, :reason, :state_json, :now)
        ON CONFLICT(key) DO UPDATE SET
            tripped     = excluded.tripped,
            tripped_at  = excluded.tripped_at,
            reason      = excluded.reason,
            state_json  = excluded.state_json,
            updated_at  = excluded.updated_at
        """,
        {
            "key": key,
            "tripped": 1 if tripped else 0,
            "tripped_at": tripped_at,
            "reason": reason,
            "state_json": state_json,
            "now": now,
        },
    )
    conn.commit()


def load_risk_state(
    conn: sqlite3.Connection, key: str
) -> dict[str, Any] | None:
    """Return the risk circuit state for *key*, or ``None`` if no row exists.

    ``tripped`` is returned as a bool.  ``state_json`` is decoded into a
    ``state`` dict; a corrupt blob is logged and replaced with ``{}``.
    """
    _ensure_row_factory(conn)
    row = conn.execute(
        "SELECT key, tripped, tripped_at, reason, state_json, updated_at "
        "FROM risk_circuit_state WHERE key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    result: dict[str, Any] = dict(row)
    result["tripped"] = bool(result["tripped"])
    try:
        result["state"] = json.loads(result.pop("state_json") or "{}")
    except json.JSONDecodeError:
        logger.warning("Corrupt state_json for risk key %s; returning empty", key)
        result["state"] = {}
    return result

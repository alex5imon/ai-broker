"""Per-strategy statistics over a rolling window of closed trades.

Pulls from the ``trades`` table. Pure aggregation — no judgement calls,
no proposals. The hypothesis layer reads these stats and decides whether
anything is worth proposing.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyStats:
    """Aggregate stats for a single strategy over a closed-trade window."""

    strategy_id: str
    window_days: int
    n_trades: int
    win_rate: float            # 0..1
    profit_factor: float | None
    total_pnl_usd: float
    avg_win_usd: float
    avg_loss_usd: float        # negative or zero
    avg_hold_minutes: float
    exit_reason_counts: dict[str, int] = field(default_factory=dict)

    def exit_share(self, reason: str) -> float:
        """Fraction of trades exited for ``reason`` (0 if none)."""
        if self.n_trades == 0:
            return 0.0
        return self.exit_reason_counts.get(reason, 0) / self.n_trades


def _parse_iso(value: str) -> datetime | None:
    try:
        # SQLite stores ISO 8601; tolerate trailing 'Z' or naive timestamps.
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def compute_window_stats(
    conn: sqlite3.Connection,
    strategy_id: str,
    window_days: int,
    *,
    now: datetime | None = None,
) -> StrategyStats:
    """Compute stats for closed trades belonging to ``strategy_id``.

    A trade is "closed" when ``exit_time IS NOT NULL`` and ``net_pnl IS NOT NULL``.
    The window is the last ``window_days`` calendar days ending at ``now``
    (UTC), inclusive of the start.
    """
    if window_days <= 0:
        raise ValueError(f"window_days must be positive, got {window_days}")

    now_utc = now or datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=window_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    cur = conn.execute(
        """
        SELECT entry_time, exit_time, exit_reason, net_pnl
          FROM trades
         WHERE strategy_id = ?
           AND exit_time IS NOT NULL
           AND net_pnl IS NOT NULL
           AND exit_time >= ?
        """,
        (strategy_id, cutoff_iso),
    )
    rows = cur.fetchall()

    if not rows:
        return StrategyStats(
            strategy_id=strategy_id,
            window_days=window_days,
            n_trades=0,
            win_rate=0.0,
            profit_factor=None,
            total_pnl_usd=0.0,
            avg_win_usd=0.0,
            avg_loss_usd=0.0,
            avg_hold_minutes=0.0,
        )

    wins: list[float] = []
    losses: list[float] = []
    hold_minutes: list[float] = []
    exit_counts: Counter[str] = Counter()
    total_pnl = 0.0

    for entry_time_str, exit_time_str, exit_reason, net_pnl in rows:
        pnl = float(net_pnl)
        total_pnl += pnl
        if pnl > 0:
            wins.append(pnl)
        else:
            losses.append(pnl)
        exit_counts[exit_reason or "unknown"] += 1

        entry_dt = _parse_iso(entry_time_str)
        exit_dt = _parse_iso(exit_time_str)
        if entry_dt and exit_dt and exit_dt > entry_dt:
            hold_minutes.append((exit_dt - entry_dt).total_seconds() / 60.0)

    n = len(rows)
    gross_wins = sum(wins)
    gross_losses_abs = abs(sum(losses))
    profit_factor: float | None
    if gross_losses_abs > 0:
        profit_factor = gross_wins / gross_losses_abs
    elif gross_wins > 0:
        profit_factor = float("inf")
    else:
        profit_factor = None

    return StrategyStats(
        strategy_id=strategy_id,
        window_days=window_days,
        n_trades=n,
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        total_pnl_usd=total_pnl,
        avg_win_usd=(gross_wins / len(wins)) if wins else 0.0,
        avg_loss_usd=(sum(losses) / len(losses)) if losses else 0.0,
        avg_hold_minutes=(sum(hold_minutes) / len(hold_minutes)) if hold_minutes else 0.0,
        exit_reason_counts=dict(exit_counts),
    )


def summarize_all(
    conn: sqlite3.Connection,
    strategy_ids: list[str],
    window_days: int,
    *,
    now: datetime | None = None,
) -> dict[str, StrategyStats]:
    """Compute stats for several strategies in one pass."""
    return {
        sid: compute_window_stats(conn, sid, window_days, now=now)
        for sid in strategy_ids
    }

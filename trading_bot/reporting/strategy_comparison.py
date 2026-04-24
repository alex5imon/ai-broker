"""Strategy comparison reporting for multi-strategy testing."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


def generate_comparison(db_path: str) -> dict[str, dict[str, Any]]:
    """Generate per-strategy performance comparison from the database."""
    conn: sqlite3.Connection = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        portfolios: list[dict[str, Any]] = [
            dict(r) for r in conn.execute("SELECT * FROM strategy_portfolios").fetchall()
        ]
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()

    result: dict[str, dict[str, Any]] = {}
    for p in portfolios:
        sid: str = p["strategy_id"]
        total: int = p.get("total_trades", 0)
        wins: int = p.get("wins", 0)
        losses: int = p.get("losses", 0)
        initial: float = p.get("initial_cash", 0)
        current: float = p.get("current_cash", 0)
        pnl: float = p.get("total_pnl", 0)

        result[sid] = {
            "display_name": p.get("display_name", sid),
            "initial_cash": initial,
            "current_cash": current,
            "total_pnl": pnl,
            "return_pct": (pnl / initial * 100) if initial > 0 else 0.0,
            "total_trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100) if total > 0 else 0.0,
            "active": bool(p.get("active", 1)),
        }

    return result


def render_comparison_text(report: dict[str, dict[str, Any]]) -> str:
    """Render a human-readable comparison table."""
    if not report:
        return "No strategy data available."

    lines: list[str] = [
        "=" * 70,
        "STRATEGY COMPARISON REPORT",
        "=" * 70,
        f"{'Strategy':<20} {'P&L':>8} {'Return':>8} {'Trades':>7} {'Win%':>7} {'Cash':>10}",
        "-" * 70,
    ]

    sorted_strategies = sorted(report.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
    for sid, data in sorted_strategies:
        lines.append(
            f"{data['display_name']:<20} "
            f"${data['total_pnl']:>7.2f} "
            f"{data['return_pct']:>7.1f}% "
            f"{data['total_trades']:>7d} "
            f"{data['win_rate']:>6.1f}% "
            f"${data['current_cash']:>9.2f}"
        )

    lines.append("-" * 70)

    # Pick winner
    if sorted_strategies:
        winner_sid, winner_data = sorted_strategies[0]
        lines.append(f"Leader: {winner_data['display_name']} ({winner_data['return_pct']:+.1f}%)")

    lines.append("=" * 70)
    return "\n".join(lines)


def pick_winner(report: dict[str, dict[str, Any]]) -> str | None:
    """Return the strategy_id with the best return."""
    if not report:
        return None
    return max(report, key=lambda sid: report[sid]["total_pnl"])

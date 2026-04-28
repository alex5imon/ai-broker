"""Volatility-targeted position sizing (concept from pysystemtrade).

The headline idea: each strategy's exposure is scaled so its **expected
per-trade move** matches a configured target. Strategies with rising
realized vol get smaller; strategies with falling realized vol get bigger
(within bounds). This dampens regime-driven swings without trying to
predict them.

This module is intentionally a small pure-function helper so it can be
called from both the live entry path (`strategy/entry.py`) and the
backtester (`multi_strategy_backtest._size_by_risk`). State (recent
returns) is stored externally — pass it in.

The math:
    target_per_trade_vol = target_annual_vol / sqrt(trades_per_year)
    multiplier = target_per_trade_vol / realized_per_trade_vol

Bounded into [min_multiplier, max_multiplier] so a single anomalous trade
can't blow up sizing.
"""

from __future__ import annotations

import logging
import math
import statistics
import sqlite3
from dataclasses import dataclass
from typing import Sequence

logger: logging.Logger = logging.getLogger(__name__)

# Trading days per year. We treat one trade per bucket as the natural
# scaling unit; per-trade vol is realized stdev of trade returns.
_TRADING_DAYS_PER_YEAR: int = 252


@dataclass(frozen=True)
class VolTargetResult:
    """Outcome of a vol-targeting calculation."""

    multiplier: float
    realized_per_trade_vol: float | None
    target_per_trade_vol: float
    sample_size: int
    reason: str

    def as_log_string(self) -> str:
        rv: str = (
            f"{self.realized_per_trade_vol:.4f}"
            if self.realized_per_trade_vol is not None
            else "n/a"
        )
        return (
            f"vol_target: x{self.multiplier:.3f} "
            f"(realized={rv}, target={self.target_per_trade_vol:.4f}, "
            f"n={self.sample_size}, {self.reason})"
        )


def vol_target_multiplier(
    trade_returns: Sequence[float],
    *,
    target_annual_vol: float,
    expected_trades_per_year: int = 252,
    min_multiplier: float = 0.5,
    max_multiplier: float = 1.5,
    min_sample: int = 10,
) -> VolTargetResult:
    """Multiplier for next-trade size given a strategy's recent returns.

    Args:
        trade_returns: Per-trade fractional returns (newest last). Pass an
            empty list to fall back to ``1.0`` (no adjustment).
        target_annual_vol: Annualized vol target as a fraction (0.20 = 20%).
        expected_trades_per_year: Used to deannualize the target. Estimate
            from the strategy's historical trade frequency.
        min_multiplier / max_multiplier: Hard bounds on the result.
        min_sample: Below this many trades, return 1.0 (no signal yet).

    Returns:
        ``VolTargetResult`` with the chosen multiplier and diagnostics.
    """
    if target_annual_vol <= 0:
        return VolTargetResult(
            multiplier=1.0,
            realized_per_trade_vol=None,
            target_per_trade_vol=0.0,
            sample_size=len(trade_returns),
            reason="target_vol<=0 (disabled)",
        )

    target_per_trade: float = target_annual_vol / math.sqrt(
        max(1, expected_trades_per_year)
    )

    n: int = len(trade_returns)
    if n < min_sample:
        return VolTargetResult(
            multiplier=1.0,
            realized_per_trade_vol=None,
            target_per_trade_vol=target_per_trade,
            sample_size=n,
            reason=f"sample<{min_sample}",
        )

    realized: float = statistics.pstdev(trade_returns)
    if realized <= 0:
        # All-zero or constant returns: no adjustment, no info.
        return VolTargetResult(
            multiplier=1.0,
            realized_per_trade_vol=realized,
            target_per_trade_vol=target_per_trade,
            sample_size=n,
            reason="realized_vol=0",
        )

    raw: float = target_per_trade / realized
    bounded: float = max(min_multiplier, min(max_multiplier, raw))
    reason: str
    if bounded != raw:
        reason = f"clamped from {raw:.3f}"
    else:
        reason = "ok"

    return VolTargetResult(
        multiplier=bounded,
        realized_per_trade_vol=realized,
        target_per_trade_vol=target_per_trade,
        sample_size=n,
        reason=reason,
    )


def load_recent_trade_returns(
    db_path: str,
    strategy_id: str,
    lookback: int = 50,
) -> list[float]:
    """Load the latest ``lookback`` per-trade fractional returns for a strategy.

    Schema-tolerant: returns ``[]`` if the trades table is missing or empty.
    """
    try:
        conn: sqlite3.Connection = sqlite3.connect(db_path)
    except sqlite3.OperationalError:
        return []
    try:
        cursor = conn.execute(
            """SELECT entry_price, quantity,
                      (CAST(quantity AS REAL) * CAST(exit_price AS REAL)
                       - CAST(quantity AS REAL) * CAST(entry_price AS REAL))
                       AS pnl
               FROM trades
               WHERE strategy_id = ?
                 AND exit_price IS NOT NULL
                 AND entry_price > 0
               ORDER BY exit_time DESC
               LIMIT ?""",
            (strategy_id, lookback),
        )
        out: list[float] = []
        for entry_price, qty, pnl in cursor.fetchall():
            try:
                cost: float = float(entry_price) * float(qty)
                if cost <= 0:
                    continue
                out.append(float(pnl) / cost)
            except (TypeError, ValueError):
                continue
        # Sort oldest-first so callers can append new returns naturally.
        out.reverse()
        return out
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

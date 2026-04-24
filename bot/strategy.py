from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

import pandas as pd
from alpaca.trading.enums import OrderSide

SYMBOLS: list[str] = ["SPY"]
LOOKBACK_MINUTES = 60 * 6  # 6 hours of 15-min bars


@dataclass(frozen=True)
class Decision:
    symbol: str
    side: OrderSide
    qty: float
    reason: str

    def client_order_id(self, strategy_name: str, bar_ts: datetime) -> str:
        raw = f"{strategy_name}|{self.symbol}|{self.side.value}|{bar_ts.isoformat()}"
        return hashlib.sha1(raw.encode()).hexdigest()[:24]


def decide(
    *,
    strategy_name: str,
    bar_ts: datetime,
    positions: dict[str, float],
    bars: dict[str, pd.DataFrame],
) -> list[Decision]:
    """Pure function: given bars, current positions, and the bar timestamp,
    return trade decisions.

    Stubbed — replace with real strategy logic. Must be deterministic for a given
    (bar_ts, positions, bars) so delayed/retried GHA runs are idempotent.
    """
    _ = (strategy_name, bar_ts, positions, bars)
    return []

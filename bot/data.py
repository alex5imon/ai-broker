from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from .config import Config


def build_data_client(cfg: Config) -> StockHistoricalDataClient:
    return StockHistoricalDataClient(cfg.api_key_id, cfg.api_secret)


def fetch_recent_bars(
    client: StockHistoricalDataClient,
    *,
    symbols: list[str],
    lookback_minutes: int,
    bar_minutes: int = 15,
    now: datetime | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch the last `lookback_minutes` of bars for each symbol.

    Returns a mapping symbol -> DataFrame indexed by UTC timestamp, columns
    [open, high, low, close, volume]. Missing symbols come back as empty frames.
    """
    if not symbols:
        return {}

    now = now or datetime.now(timezone.utc)
    start = now - timedelta(minutes=lookback_minutes)

    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame(bar_minutes, TimeFrameUnit.Minute),
        start=start,
        end=now,
        feed=DataFeed.IEX,
    )
    resp = client.get_stock_bars(req).df

    result: dict[str, pd.DataFrame] = {s: pd.DataFrame() for s in symbols}
    if resp is None or resp.empty:
        return result

    # alpaca-py returns a MultiIndex (symbol, timestamp)
    if isinstance(resp.index, pd.MultiIndex):
        for sym in symbols:
            if sym in resp.index.get_level_values(0):
                result[sym] = resp.xs(sym, level=0).copy()
    else:
        # single-symbol case
        result[symbols[0]] = resp.copy()

    return result

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

import pandas as pd

from .config import load_config
from .data import build_data_client, fetch_recent_bars
from .strategy import LOOKBACK_MINUTES, SYMBOLS, decide

STRATEGY_NAME = "default"


def run_backtest(start: str, end: str) -> None:
    start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)

    cfg = load_config()
    data_client = build_data_client(cfg)

    # One-shot fetch of the full range, then walk forward bar-by-bar.
    full = fetch_recent_bars(
        data_client,
        symbols=SYMBOLS,
        lookback_minutes=int((end_dt - start_dt).total_seconds() // 60) + LOOKBACK_MINUTES,
        bar_minutes=15,
        now=end_dt,
    )

    positions: dict[str, float] = {}
    total = 0
    bar_ts = start_dt
    while bar_ts <= end_dt:
        window: dict[str, pd.DataFrame] = {
            s: df.loc[df.index <= bar_ts].tail(LOOKBACK_MINUTES // 15)
            for s, df in full.items()
        }
        decisions = decide(
            strategy_name=STRATEGY_NAME,
            bar_ts=bar_ts,
            positions=positions,
            bars=window,
        )
        total += len(decisions)
        bar_ts += pd.Timedelta(minutes=15)

    print(f"backtest {start}..{end}: {total} decisions (stub)")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="YYYY-MM-DDTHH:MM")
    p.add_argument("--end", required=True, help="YYYY-MM-DDTHH:MM")
    args = p.parse_args()
    run_backtest(args.start, args.end)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""ORB-on-stocks-in-play — Spike 1 (sample-rate sanity check).

Issue #138. The paper (Zarattini & Aziz 2023) ranks the top 20 names by
*relative opening volume*: first_5min_volume / mean(first_5min_volume,
last 14 days). Our 13-ETF universe is much smaller — this script counts
how many trading signals the volume filter alone would emit per week so
we know whether the ETF version is sample-starved.

Decision gate from the ticket:
- If fewer than ~3 trades/week across the basket → ETF version is
  sample-starved; widen to individual stocks or kill the idea.
- Otherwise → proceed to Spike 2 (single-instrument ORB backtest).

Run from the project root (or the worktree with data/ symlinked):

    python -m trading_bot.docs.orb_stocks_in_play.spike1_sample_rate

Reads 1-min parquet cache at data/cache/{TICKER}/{DATE}_intraday.parquet.
No code changes. No API calls. Pure offline analysis.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("US/Eastern")
UTC = ZoneInfo("UTC")

# Match the universe the live bot trades (13 ETFs)
UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "XLF", "XLK", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "XLC",
)

# Project root resolved relative to this file: trading_bot/docs/orb_stocks_in_play/spike1_*
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

# Top-N selection for the universe (paper uses top-20 of universe with thousands
# of stocks; with 13 ETFs an equivalent top-quintile-ish slice is top-2 or top-3).
TOP_N: int = 2

# Alternative: absolute ratio threshold. The paper's top-20 cut typically lands
# around relative_vol >= 1.5. We also count under this less-strict variant.
RATIO_THRESHOLD: float = 1.5

# Rolling lookback for the baseline mean
LOOKBACK_DAYS: int = 14


def _first_5min_volume(df_1min: pd.DataFrame) -> float | None:
    """Sum of the first 5 1-min bars at/after 09:30 ET. None if not enough
    bars or the open isn't covered by the parquet."""
    if df_1min is None or df_1min.empty:
        return None
    # Index is tz-aware UTC. Convert to ET, filter 09:30:00 <= t < 09:35:00.
    idx_et = df_1min.index.tz_convert(ET)
    mask = (idx_et.time >= pd.Timestamp("09:30").time()) & (
        idx_et.time < pd.Timestamp("09:35").time()
    )
    head = df_1min.loc[mask]
    if len(head) < 5:
        return None
    return float(head["volume"].iloc[:5].sum())


def _list_trading_days(ticker: str) -> list[date]:
    tdir = CACHE_DIR / ticker
    if not tdir.exists():
        return []
    days: list[date] = []
    for p in tdir.glob("*_intraday.parquet"):
        try:
            days.append(date.fromisoformat(p.stem.split("_")[0]))
        except ValueError:
            continue
    days.sort()
    return days


def _load_volumes(ticker: str) -> dict[date, float]:
    """ticker -> {date: first_5min_volume} (drops days with insufficient bars)."""
    out: dict[date, float] = {}
    for d in _list_trading_days(ticker):
        df = pd.read_parquet(CACHE_DIR / ticker / f"{d.isoformat()}_intraday.parquet")
        v = _first_5min_volume(df)
        if v is not None and v > 0:
            out[d] = v
    return out


def _compute_ratios(volumes: dict[date, float]) -> dict[date, float]:
    """For each date, ratio = today / mean(last 14 *previous* trading days)."""
    sorted_days = sorted(volumes)
    ratios: dict[date, float] = {}
    for i in range(LOOKBACK_DAYS, len(sorted_days)):
        today = sorted_days[i]
        prev = sorted_days[i - LOOKBACK_DAYS : i]
        prev_mean = sum(volumes[d] for d in prev) / LOOKBACK_DAYS
        if prev_mean > 0:
            ratios[today] = volumes[today] / prev_mean
    return ratios


def main() -> int:
    # Load everything up front
    print(f"Loading 1-min cache for {len(UNIVERSE)} tickers...", flush=True)
    ratios_by_ticker: dict[str, dict[date, float]] = {}
    for t in UNIVERSE:
        v = _load_volumes(t)
        if not v:
            print(f"  {t}: NO DATA — skipping", flush=True)
            continue
        ratios_by_ticker[t] = _compute_ratios(v)
        print(
            f"  {t}: {len(v)} cached days, {len(ratios_by_ticker[t])} days with ratio",
            flush=True,
        )

    if not ratios_by_ticker:
        print("\nNo data found in cache. Aborting.", file=sys.stderr)
        return 1

    # Reshape: date -> {ticker: ratio}
    by_date: dict[date, dict[str, float]] = defaultdict(dict)
    for t, rs in ratios_by_ticker.items():
        for d, r in rs.items():
            by_date[d][t] = r

    sorted_dates = sorted(by_date)
    if not sorted_dates:
        print("No dated ratios.", file=sys.stderr)
        return 1
    print(
        f"\nDate range: {sorted_dates[0]} → {sorted_dates[-1]}"
        f" ({len(sorted_dates)} trading days)\n",
        flush=True,
    )

    # Variant 1: top-N selection per day
    top_n_trades = 0
    for d, rs in by_date.items():
        ranked = sorted(rs.items(), key=lambda kv: kv[1], reverse=True)
        top_n_trades += min(TOP_N, len(ranked))

    # Variant 2: absolute threshold (ratio >= RATIO_THRESHOLD)
    threshold_trades = 0
    threshold_per_day: list[int] = []
    for d in sorted_dates:
        rs = by_date[d]
        hits = sum(1 for r in rs.values() if r >= RATIO_THRESHOLD)
        threshold_trades += hits
        threshold_per_day.append(hits)

    weeks = (sorted_dates[-1] - sorted_dates[0]).days / 7.0
    if weeks <= 0:
        weeks = 1.0

    print(f"=== Variant 1: top-{TOP_N} per day ===")
    print(f"  Total signal-days: {top_n_trades}")
    print(f"  Per week: {top_n_trades / weeks:.2f}")

    print(f"\n=== Variant 2: ratio >= {RATIO_THRESHOLD} ===")
    print(f"  Total signal-days: {threshold_trades}")
    print(f"  Per week: {threshold_trades / weeks:.2f}")
    if threshold_per_day:
        s = pd.Series(threshold_per_day)
        print(
            f"  Per day: min={s.min()} p50={s.median():.1f} mean={s.mean():.2f}"
            f" p90={s.quantile(0.9):.1f} max={s.max()}",
        )

    print(f"\n=== Per-ticker signal rate (ratio >= {RATIO_THRESHOLD}) ===")
    print(f"{'Ticker':>6s} {'Days w/ratio':>13s} {'Hits':>6s} {'Hit rate':>9s}")
    for t in sorted(ratios_by_ticker):
        rs = ratios_by_ticker[t]
        hits = sum(1 for r in rs.values() if r >= RATIO_THRESHOLD)
        rate = (hits / len(rs)) if rs else 0.0
        print(f"{t:>6s} {len(rs):>13d} {hits:>6d} {rate * 100:>8.1f}%")

    print("\n=== Decision gate (ticket #138 Spike 1) ===")
    print(f"  Top-{TOP_N} variant: {top_n_trades / weeks:.2f} trades/week")
    print(f"  Threshold variant: {threshold_trades / weeks:.2f} trades/week")
    print(f"  Bar: \"~3 trades/week across the basket\"")
    gate_pass = (top_n_trades / weeks >= 3.0) or (threshold_trades / weeks >= 3.0)
    print(f"  Verdict: {'PASS — proceed to Spike 2' if gate_pass else 'FAIL — sample-starved'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

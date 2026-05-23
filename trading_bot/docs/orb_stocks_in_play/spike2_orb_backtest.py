"""ORB-on-stocks-in-play — Spike 2 (single-instrument feasibility).

Issue #138. Spike 1 (sample_rate) confirmed the volume filter emits
enough signals; Spike 2 confirms (or refutes) that the filter actually
*helps*. Compares two variants on SPY 1-min bars:

  A. naive ORB                : enter on every 5-min ORB break
  B. stocks-in-play ORB       : enter only when today's first-5-min
                                volume / mean(last 14 trading days' first-5-min
                                volume) >= 1.5

Strategy (both variants):
  - First 5 1-min bars (09:30-09:34 ET) define opening range (OR_high, OR_low)
  - From 09:35 ET to 15:30 ET (entry cutoff), enter LONG at the first
    1-min bar whose high crosses OR_high. Fill = OR_high (conservative).
  - Stop = OR_low. Target = OR_high + 1.0 * (OR_high - OR_low) (1R).
  - Exit on stop, target, or 15:55 close.
  - One entry per day. No re-entries.
  - Sizing: 1% portfolio risk per trade (risk = OR_high - OR_low),
    capped by max-position-pct * equity / entry_price.

Output: side-by-side comparison of trades, WR, PF, return, MaxDD.
Decision gate: stocks-in-play variant must materially beat naive ORB
(higher PF, return, or lower DD) for the volume filter to earn its
complexity. Otherwise the filter is dead weight.

Run:

    python -m trading_bot.docs.orb_stocks_in_play.spike2_orb_backtest

Standalone — no API calls, reads only the 1-min parquet cache.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("US/Eastern")

# Tunables
TICKER: str = "SPY"
RANGE_MINUTES: int = 5
ENTRY_CUTOFF_ET: time = time(15, 30)
WIND_DOWN_ET: time = time(15, 55)
TARGET_R_MULTIPLE: float = 1.0
RISK_PER_TRADE_PCT: float = 0.01
MAX_POSITION_PCT: float = 0.95
INITIAL_EQUITY_USD: float = 2500.0

VOLUME_RATIO_THRESHOLD: float = 1.5
VOLUME_LOOKBACK_DAYS: int = 14

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"


@dataclass
class Trade:
    entry_date: date
    entry_time: datetime
    entry_price: float
    stop_price: float
    target_price: float
    shares: float
    exit_time: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.shares


@dataclass
class Result:
    label: str
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    starting_equity: float = INITIAL_EQUITY_USD

    @property
    def ending_equity(self) -> float:
        return self.starting_equity + sum(t.pnl for t in self.trades)

    @property
    def return_pct(self) -> float:
        return (self.ending_equity - self.starting_equity) / self.starting_equity * 100

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.pnl > 0)
        return wins / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl <= 0)
        return gains / losses if losses > 0 else float("inf") if gains > 0 else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.starting_equity
        max_dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                dd = (peak - eq) / peak * 100
                max_dd = max(max_dd, dd)
        return max_dd


def _load_day(d: date) -> pd.DataFrame | None:
    p = CACHE_DIR / TICKER / f"{d.isoformat()}_intraday.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    if df.empty:
        return None
    return df


def _list_trading_days() -> list[date]:
    tdir = CACHE_DIR / TICKER
    days: list[date] = []
    for p in tdir.glob("*_intraday.parquet"):
        try:
            days.append(date.fromisoformat(p.stem.split("_")[0]))
        except ValueError:
            continue
    days.sort()
    return days


def _first_5min_volume(df: pd.DataFrame) -> float | None:
    idx_et = df.index.tz_convert(ET)
    mask = (idx_et.time >= time(9, 30)) & (idx_et.time < time(9, 35))
    head = df.loc[mask]
    if len(head) < 5:
        return None
    return float(head["volume"].iloc[:5].sum())


def _opening_range(df: pd.DataFrame) -> tuple[float, float] | None:
    """Return (high, low) of the first RANGE_MINUTES 1-min bars."""
    idx_et = df.index.tz_convert(ET)
    end_t = time(9, 30 + RANGE_MINUTES)
    mask = (idx_et.time >= time(9, 30)) & (idx_et.time < end_t)
    rng = df.loc[mask]
    if len(rng) < RANGE_MINUTES:
        return None
    return float(rng["high"].max()), float(rng["low"].min())


def _simulate_day(df: pd.DataFrame, equity: float) -> Trade | None:
    """One ORB long trade for this day; None if no entry signal."""
    rng = _opening_range(df)
    if rng is None:
        return None
    or_high, or_low = rng
    range_size = or_high - or_low
    if range_size <= 0:
        return None

    target = or_high + TARGET_R_MULTIPLE * range_size
    stop = or_low

    # Sizing
    risk_per_share = or_high - or_low
    if risk_per_share <= 0:
        return None
    risk_dollars = equity * RISK_PER_TRADE_PCT
    shares_by_risk = risk_dollars / risk_per_share
    shares_by_cap = (equity * MAX_POSITION_PCT) / or_high
    shares = round(min(shares_by_risk, shares_by_cap), 4)
    if shares <= 0.001:
        return None

    idx_et = df.index.tz_convert(ET)
    range_end_t = time(9, 30 + RANGE_MINUTES)
    after_range_mask = (idx_et.time >= range_end_t) & (idx_et.time <= ENTRY_CUTOFF_ET)
    after = df.loc[after_range_mask]
    if after.empty:
        return None

    # Find the first bar whose high breaks above OR_high (entry trigger)
    breakouts = after[after["high"] > or_high]
    if breakouts.empty:
        return None
    entry_ts = breakouts.index[0]
    # Fill at OR_high (conservative — assumes stop order at the level)
    entry_dt = entry_ts.tz_convert(ET).to_pydatetime()

    trade = Trade(
        entry_date=entry_dt.date(),
        entry_time=entry_dt,
        entry_price=or_high,
        stop_price=stop,
        target_price=target,
        shares=shares,
    )

    # Walk subsequent bars looking for stop / target / wind-down
    subsequent = df.loc[df.index > entry_ts]
    sub_et = subsequent.index.tz_convert(ET)
    for ts, row in subsequent.iterrows():
        bar_t = ts.tz_convert(ET).time()
        if bar_t > WIND_DOWN_ET:
            trade.exit_time = ts.tz_convert(ET).to_pydatetime()
            trade.exit_price = float(row["close"])
            trade.exit_reason = "wind_down"
            return trade
        # Stop hit (intrabar low)
        if float(row["low"]) <= stop:
            trade.exit_time = ts.tz_convert(ET).to_pydatetime()
            trade.exit_price = stop
            trade.exit_reason = "stop_loss"
            return trade
        # Target hit (intrabar high)
        if float(row["high"]) >= target:
            trade.exit_time = ts.tz_convert(ET).to_pydatetime()
            trade.exit_price = target
            trade.exit_reason = "take_profit"
            return trade

    # No bar matched — close at the last bar
    last_ts = subsequent.index[-1]
    trade.exit_time = last_ts.tz_convert(ET).to_pydatetime()
    trade.exit_price = float(subsequent.iloc[-1]["close"])
    trade.exit_reason = "session_end"
    return trade


def _format_summary(r: Result) -> str:
    return (
        f"{r.label:<28s} "
        f"trades={r.num_trades:>4d}  "
        f"WR={r.win_rate:>5.1f}%  "
        f"PF={r.profit_factor:>6.3f}  "
        f"Return={r.return_pct:>+7.2f}%  "
        f"MaxDD={r.max_drawdown_pct:>5.2f}%  "
        f"FinalEq=${r.ending_equity:>8.2f}"
    )


def main() -> int:
    all_days = _list_trading_days()
    if not all_days:
        print(f"No cache for {TICKER}", file=sys.stderr)
        return 1

    # First pass: compute per-day first_5min_volume to build the in-play mask
    first5: dict[date, float] = {}
    print(f"Pass 1: computing first_5min_volume across {len(all_days)} days...", flush=True)
    for d in all_days:
        df = _load_day(d)
        if df is None:
            continue
        v = _first_5min_volume(df)
        if v is not None and v > 0:
            first5[d] = v

    # Compute in-play mask using a rolling 14-day baseline (excludes today)
    sorted_days = sorted(first5)
    in_play: dict[date, bool] = {}
    for i in range(VOLUME_LOOKBACK_DAYS, len(sorted_days)):
        today = sorted_days[i]
        prev = sorted_days[i - VOLUME_LOOKBACK_DAYS : i]
        prev_mean = sum(first5[d] for d in prev) / VOLUME_LOOKBACK_DAYS
        if prev_mean > 0:
            in_play[today] = (first5[today] / prev_mean) >= VOLUME_RATIO_THRESHOLD

    candidate_days = [d for d in sorted_days if d in in_play]
    n_in_play = sum(1 for d in candidate_days if in_play[d])
    print(
        f"  → {len(candidate_days)} eligible trading days (post warm-up); "
        f"{n_in_play} flagged in-play ({n_in_play / len(candidate_days) * 100:.1f}%)",
        flush=True,
    )

    # Pass 2: simulate both variants in lockstep so the equity curves are comparable
    naive = Result(label=f"Naive ORB ({TICKER})")
    inplay = Result(label=f"Stocks-in-play ORB ({TICKER})")

    print(f"\nPass 2: backtesting both variants on {len(candidate_days)} days...", flush=True)
    for d in candidate_days:
        df = _load_day(d)
        if df is None:
            continue

        # Naive: always attempt entry
        eq_naive = naive.starting_equity + sum(t.pnl for t in naive.trades)
        tr_n = _simulate_day(df, eq_naive)
        if tr_n is not None:
            naive.trades.append(tr_n)
        naive.equity_curve.append(
            (d, naive.starting_equity + sum(t.pnl for t in naive.trades))
        )

        # In-play: only attempt on flagged days
        eq_in = inplay.starting_equity + sum(t.pnl for t in inplay.trades)
        if in_play.get(d, False):
            tr_i = _simulate_day(df, eq_in)
            if tr_i is not None:
                inplay.trades.append(tr_i)
        inplay.equity_curve.append(
            (d, inplay.starting_equity + sum(t.pnl for t in inplay.trades))
        )

    # Output
    print("\n" + "=" * 90)
    print(f"Spike 2 results — SPY 1-min, {candidate_days[0]} → {candidate_days[-1]}")
    print("=" * 90)
    print(_format_summary(naive))
    print(_format_summary(inplay))
    print("=" * 90)

    # Side-by-side deltas
    print(f"\nDelta (in-play - naive):")
    print(f"  Return: {inplay.return_pct - naive.return_pct:+.2f} pp")
    print(f"  WR: {inplay.win_rate - naive.win_rate:+.2f} pp")
    print(f"  PF: {inplay.profit_factor - naive.profit_factor:+.3f}")
    print(f"  MaxDD: {inplay.max_drawdown_pct - naive.max_drawdown_pct:+.2f} pp")
    print(f"  Trade count: {inplay.num_trades - naive.num_trades:+d}")

    # Per-day in-play vs not, naive WR comparison (does the filter pick winners?)
    in_play_trade_pnls: list[float] = []
    out_play_trade_pnls: list[float] = []
    for t in naive.trades:
        if in_play.get(t.entry_date, False):
            in_play_trade_pnls.append(t.pnl)
        else:
            out_play_trade_pnls.append(t.pnl)
    n_in = len(in_play_trade_pnls)
    n_out = len(out_play_trade_pnls)
    win_in = sum(1 for p in in_play_trade_pnls if p > 0)
    win_out = sum(1 for p in out_play_trade_pnls if p > 0)
    pnl_in = sum(in_play_trade_pnls)
    pnl_out = sum(out_play_trade_pnls)
    print(f"\n=== Naive trades split by in-play flag ===")
    print(f"  In-play days: n={n_in:>4d}  wins={win_in:>4d}  WR={(win_in / n_in * 100) if n_in else 0:>5.1f}%  total_pnl=${pnl_in:>8.2f}  avg=${(pnl_in / n_in) if n_in else 0:>6.2f}")
    print(f"  Other days:   n={n_out:>4d}  wins={win_out:>4d}  WR={(win_out / n_out * 100) if n_out else 0:>5.1f}%  total_pnl=${pnl_out:>8.2f}  avg=${(pnl_out / n_out) if n_out else 0:>6.2f}")

    print(f"\n=== Decision gate (Spike 2 — does the filter add edge?) ===")
    pf_better = inplay.profit_factor > naive.profit_factor
    ret_better = inplay.return_pct > naive.return_pct
    dd_better = inplay.max_drawdown_pct < naive.max_drawdown_pct
    margin = (
        (inplay.return_pct - naive.return_pct) > 0
        and (inplay.profit_factor - naive.profit_factor) > 0
    )
    print(f"  In-play PF > naive PF?       {'YES' if pf_better else 'no '}  ({inplay.profit_factor:.3f} vs {naive.profit_factor:.3f})")
    print(f"  In-play return > naive?      {'YES' if ret_better else 'no '}  ({inplay.return_pct:+.2f}% vs {naive.return_pct:+.2f}%)")
    print(f"  In-play MaxDD < naive MaxDD? {'YES' if dd_better else 'no '}  ({inplay.max_drawdown_pct:.2f}% vs {naive.max_drawdown_pct:.2f}%)")
    print(f"  Both PF and return better?   {'YES — proceed to Spike 3' if margin else 'no — filter does not earn its complexity'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

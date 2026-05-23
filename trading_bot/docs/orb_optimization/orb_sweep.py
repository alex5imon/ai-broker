"""ORB parameter sweep on SPY — exploring the side-finding from #138 Spike 2.

Spike 2 (closed #138) found the naive ORB on SPY has modest positive
expectancy: PF 1.142, 52.6% WR, +16.23% over 5.7 years, MaxDD 5.29%.
This script explores whether parameter tuning can lift that to
production-shippable.

Phase 1 (this script): one-factor-at-a-time sweep around the Spike 2
baseline. Identifies which knobs matter. No in-sample/out-of-sample
split — Phase 1 is diagnostic.

Phase 2 (separate run): grid-search the top knobs with IS/OOS split.

Run:

    python -m trading_bot.docs.orb_optimization.orb_sweep

Reads 1-min cache at data/cache/SPY/. No API calls.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("US/Eastern")
PROJECT_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = PROJECT_ROOT / "data" / "cache"

TICKER: str = "SPY"
INITIAL_EQUITY: float = 2500.0


@dataclass
class Cfg:
    range_minutes: int = 5
    target_r: float | None = 1.0
    entry_cutoff_et: time = time(15, 30)
    wind_down_et: time = time(15, 55)
    min_range_pct: float = 0.0
    risk_per_trade_pct: float = 0.01
    max_position_pct: float = 0.95
    label: str = "baseline"


@dataclass
class TradeRecord:
    entry_date: date
    entry_price: float
    stop_price: float
    target_price: float | None
    shares: float
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
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[tuple[date, float]] = field(default_factory=list)
    starting_equity: float = INITIAL_EQUITY

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
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades) * 100

    @property
    def profit_factor(self) -> float:
        g = sum(t.pnl for t in self.trades if t.pnl > 0)
        l = -sum(t.pnl for t in self.trades if t.pnl <= 0)
        return g / l if l > 0 else (float("inf") if g > 0 else 0.0)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.starting_equity
        max_dd = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100)
        return max_dd

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        return sum(losses) / len(losses) if losses else 0.0


def _list_trading_days(ticker: str = TICKER) -> list[date]:
    tdir = CACHE_DIR / ticker
    days: list[date] = []
    for p in tdir.glob("*_intraday.parquet"):
        try:
            days.append(date.fromisoformat(p.stem.split("_")[0]))
        except ValueError:
            continue
    days.sort()
    return days


def _load_day(d: date, ticker: str = TICKER) -> pd.DataFrame | None:
    p = CACHE_DIR / ticker / f"{d.isoformat()}_intraday.parquet"
    if not p.exists():
        return None
    df = pd.read_parquet(p)
    return df if not df.empty else None


def _simulate_day(df: pd.DataFrame, cfg: Cfg, equity: float) -> TradeRecord | None:
    idx_et = df.index.tz_convert(ET)
    range_end_t = (
        datetime.combine(date.today(), time(9, 30))
        + timedelta(minutes=cfg.range_minutes)
    ).time()

    # Opening range
    rng_mask = (idx_et.time >= time(9, 30)) & (idx_et.time < range_end_t)
    rng = df.loc[rng_mask]
    if len(rng) < cfg.range_minutes:
        return None
    or_high = float(rng["high"].max())
    or_low = float(rng["low"].min())
    range_size = or_high - or_low
    if range_size <= 0:
        return None

    mid_price = (or_high + or_low) / 2
    if cfg.min_range_pct > 0 and range_size / mid_price < cfg.min_range_pct:
        return None

    stop = or_low
    target = (or_high + cfg.target_r * range_size) if cfg.target_r else None

    # Sizing
    risk_per_share = or_high - stop
    if risk_per_share <= 0:
        return None
    risk_dollars = equity * cfg.risk_per_trade_pct
    shares_by_risk = risk_dollars / risk_per_share
    shares_by_cap = (equity * cfg.max_position_pct) / or_high
    shares = round(min(shares_by_risk, shares_by_cap), 4)
    if shares <= 0.001:
        return None

    # Look for entry trigger in [range_end, entry_cutoff]
    entry_mask = (idx_et.time >= range_end_t) & (idx_et.time <= cfg.entry_cutoff_et)
    after = df.loc[entry_mask]
    breakouts = after[after["high"] > or_high]
    if breakouts.empty:
        return None
    entry_ts = breakouts.index[0]

    trade = TradeRecord(
        entry_date=entry_ts.tz_convert(ET).date(),
        entry_price=or_high,
        stop_price=stop,
        target_price=target,
        shares=shares,
    )

    # Walk subsequent bars looking for stop / target / wind-down
    subsequent = df.loc[df.index > entry_ts]
    for ts, row in subsequent.iterrows():
        bar_t = ts.tz_convert(ET).time()
        if bar_t > cfg.wind_down_et:
            trade.exit_price = float(row["close"])
            trade.exit_reason = "wind_down"
            return trade
        if float(row["low"]) <= stop:
            trade.exit_price = stop
            trade.exit_reason = "stop_loss"
            return trade
        if target is not None and float(row["high"]) >= target:
            trade.exit_price = target
            trade.exit_reason = "take_profit"
            return trade

    if not subsequent.empty:
        trade.exit_price = float(subsequent.iloc[-1]["close"])
        trade.exit_reason = "session_end"
    return trade


def run_backtest(cfg: Cfg, days: list[date], ticker: str = TICKER) -> Result:
    r = Result(label=cfg.label)
    for d in days:
        df = _load_day(d, ticker)
        if df is None:
            continue
        eq = r.starting_equity + sum(t.pnl for t in r.trades)
        tr = _simulate_day(df, cfg, eq)
        if tr is not None:
            r.trades.append(tr)
        r.equity_curve.append((d, r.starting_equity + sum(t.pnl for t in r.trades)))
    return r


def fmt_row(r: Result) -> str:
    return (
        f"{r.label:<28s}  "
        f"trades={r.num_trades:>4d}  "
        f"WR={r.win_rate:>5.1f}%  "
        f"PF={r.profit_factor:>6.3f}  "
        f"Ret={r.return_pct:>+7.2f}%  "
        f"DD={r.max_drawdown_pct:>5.2f}%  "
        f"AvgW=${r.avg_win:>6.2f}  "
        f"AvgL=${r.avg_loss:>6.2f}"
    )


def main() -> int:
    days = _list_trading_days()
    if not days:
        print("No SPY cache", file=sys.stderr)
        return 1
    print(f"SPY cache: {len(days)} days, {days[0]} → {days[-1]}\n", flush=True)

    baseline = Cfg(label="baseline (5min/1R/15:30)")
    print("Baseline (Spike 2):")
    bres = run_backtest(baseline, days)
    print("  " + fmt_row(bres))
    print()

    # Sweep 1: range_minutes
    print("Sweep 1: range_minutes (target_r=1.0, cutoff=15:30)")
    for rm in (5, 10, 15, 30, 60):
        cfg = Cfg(range_minutes=rm, label=f"range={rm}min")
        print("  " + fmt_row(run_backtest(cfg, days)))
    print()

    # Sweep 2: target_r
    print("Sweep 2: target_r (range=5min, cutoff=15:30)")
    for tr in (0.5, 1.0, 1.5, 2.0, 3.0, None):
        lbl = f"target=R{tr}" if tr is not None else "target=close"
        cfg = Cfg(target_r=tr, label=lbl)
        print("  " + fmt_row(run_backtest(cfg, days)))
    print()

    # Sweep 3: entry_cutoff
    print("Sweep 3: entry_cutoff (range=5min, target_r=1.0)")
    for hr, mn in [(10, 30), (11, 0), (12, 0), (13, 0), (14, 0), (15, 30)]:
        cfg = Cfg(entry_cutoff_et=time(hr, mn), label=f"cutoff={hr:02d}:{mn:02d}")
        print("  " + fmt_row(run_backtest(cfg, days)))
    print()

    # Sweep 4: min_range_pct (skip too-tight ranges)
    print("Sweep 4: min_range_pct (range=5min, target_r=1.0, cutoff=15:30)")
    for mr in (0.0, 0.001, 0.002, 0.003, 0.005, 0.008):
        cfg = Cfg(min_range_pct=mr, label=f"min_range={mr * 100:.1f}%")
        print("  " + fmt_row(run_backtest(cfg, days)))
    print()

    # Sweep 5: combined heuristic — wider range + bigger target
    print("Sweep 5: paired combos (best-of-OFAT guesses)")
    combos = [
        Cfg(range_minutes=15, target_r=2.0, label="15min/R2"),
        Cfg(range_minutes=15, target_r=1.5, label="15min/R1.5"),
        Cfg(range_minutes=15, target_r=1.0, label="15min/R1"),
        Cfg(range_minutes=30, target_r=2.0, label="30min/R2"),
        Cfg(range_minutes=30, target_r=1.5, label="30min/R1.5"),
        Cfg(range_minutes=15, target_r=None, label="15min/close"),
        Cfg(range_minutes=30, target_r=None, label="30min/close"),
    ]
    for c in combos:
        print("  " + fmt_row(run_backtest(c, days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

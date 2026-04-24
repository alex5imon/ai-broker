"""Validation suite for the mean reversion strategy.

Runs on the latest full-window backtest trade log:
1. Yearly P&L breakdown
2. Monte Carlo bootstrap on trade order (10,000 permutations)
3. Trade stats by ticker, by exit reason
4. Rolling 30-trade win rate
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

RESULTS_PATH = Path(
    "backtest_results/"
    "multi_strategy_2020-07-27_to_2026-04-16_20260420T162929.json"
)


def load_trades(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    strat = data["strategies"]
    if isinstance(strat, dict):
        strat = strat.get("mean_reversion", list(strat.values())[0])
    elif isinstance(strat, list):
        strat = strat[0]
    return strat["trades"]


def yearly_breakdown(trades: list[dict]) -> None:
    print("\n=== YEARLY P&L BREAKDOWN ===")
    by_year: dict[int, list[float]] = defaultdict(list)
    for t in trades:
        year = datetime.fromisoformat(t["exit_time"]).year
        by_year[year].append(t["pnl_usd"])
    print(f"{'Year':<6} {'Trades':>7} {'Wins':>5} {'Win%':>6} {'P&L':>10} {'AvgPnL':>8}")
    for y in sorted(by_year):
        pnls = by_year[y]
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        wr = 100 * wins / len(pnls)
        print(f"{y:<6} {len(pnls):>7} {wins:>5} {wr:>5.1f}% "
              f"${total:>+9.2f} ${total/len(pnls):>+7.2f}")


def ticker_breakdown(trades: list[dict]) -> None:
    print("\n=== P&L BY TICKER ===")
    by_tkr: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_tkr[t["ticker"]].append(t["pnl_usd"])
    print(f"{'Ticker':<7} {'Trades':>7} {'Win%':>6} {'P&L':>10} {'AvgPnL':>8}")
    rows = []
    for tk, pnls in by_tkr.items():
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        wr = 100 * wins / len(pnls)
        rows.append((total, tk, len(pnls), wr, total))
    for _, tk, n, wr, total in sorted(rows, reverse=True):
        print(f"{tk:<7} {n:>7} {wr:>5.1f}% ${total:>+9.2f} ${total/n:>+7.2f}")


def exit_reason_breakdown(trades: list[dict]) -> None:
    print("\n=== P&L BY EXIT REASON ===")
    by_r: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_r[t["exit_reason"]].append(t["pnl_usd"])
    print(f"{'Reason':<25} {'Trades':>7} {'Win%':>6} {'P&L':>10} {'AvgPnL':>8}")
    for r, pnls in sorted(by_r.items(), key=lambda kv: -sum(kv[1])):
        wins = sum(1 for p in pnls if p > 0)
        total = sum(pnls)
        wr = 100 * wins / len(pnls)
        print(f"{r:<25} {len(pnls):>7} {wr:>5.1f}% ${total:>+9.2f} ${total/len(pnls):>+7.2f}")


def monte_carlo(trades: list[dict], initial_cash: float = 1000.0, n_sims: int = 10_000) -> None:
    """Shuffle trade order; compute DD distribution.

    Reveals whether the observed -13.7% realized DD was lucky trade ordering,
    or structural to the strategy's return profile.
    """
    print(f"\n=== MONTE CARLO BOOTSTRAP ({n_sims:,} sims, shuffled trade order) ===")
    pnls = np.array([t["pnl_usd"] for t in trades])
    rng = np.random.default_rng(42)

    final_values: list[float] = []
    max_dds: list[float] = []
    n_negative: int = 0

    for _ in range(n_sims):
        shuffled = rng.permutation(pnls)
        equity = initial_cash + np.cumsum(shuffled)
        running_peak = np.maximum.accumulate(np.concatenate([[initial_cash], equity]))
        dd = (equity - running_peak[1:]) / running_peak[1:]
        final_values.append(equity[-1])
        max_dds.append(dd.min())
        if equity[-1] < initial_cash:
            n_negative += 1

    fv = np.array(final_values)
    dd = np.array(max_dds)

    print(f"Final equity distribution (from ${initial_cash:.0f}):")
    print(f"  P5:    ${np.percentile(fv, 5):.2f}  ({100*(np.percentile(fv,5)/initial_cash - 1):+.1f}%)")
    print(f"  P25:   ${np.percentile(fv, 25):.2f}  ({100*(np.percentile(fv,25)/initial_cash - 1):+.1f}%)")
    print(f"  P50:   ${np.percentile(fv, 50):.2f}  ({100*(np.percentile(fv,50)/initial_cash - 1):+.1f}%)")
    print(f"  P75:   ${np.percentile(fv, 75):.2f}  ({100*(np.percentile(fv,75)/initial_cash - 1):+.1f}%)")
    print(f"  P95:   ${np.percentile(fv, 95):.2f}  ({100*(np.percentile(fv,95)/initial_cash - 1):+.1f}%)")
    print(f"\nMax drawdown distribution:")
    print(f"  P5  (best):  {100*np.percentile(dd, 95):.1f}%")
    print(f"  P50:         {100*np.percentile(dd, 50):.1f}%")
    print(f"  P95 (worst): {100*np.percentile(dd, 5):.1f}%")
    print(f"\nP(ending net-negative over this many trades): "
          f"{100*n_negative/n_sims:.2f}%")


def forward_bootstrap(trades: list[dict], initial_cash: float = 1000.0,
                       forward_trades: int = 60, n_sims: int = 10_000) -> None:
    """Bootstrap with replacement to simulate forward windows.

    299 trades over 5.7yrs ≈ 52 trades/year ≈ 13 trades/quarter per universe.
    But with 13 tickers the rate is higher. Simulate different horizons.
    """
    print(f"\n=== FORWARD BOOTSTRAP (bootstrap-with-replacement) ===")
    pnls = np.array([t["pnl_usd"] for t in trades])
    rng = np.random.default_rng(42)

    for n_fwd in [30, 60, 120, 260]:
        final_pnl = np.array([
            rng.choice(pnls, size=n_fwd, replace=True).sum()
            for _ in range(n_sims)
        ])
        p_negative = (final_pnl < 0).mean() * 100
        p_down_5 = (final_pnl < -initial_cash * 0.05).mean() * 100
        months_approx = n_fwd / 52 * 12 / 13 * 13  # rough month proxy
        print(f"  Forward window of {n_fwd} trades (~{n_fwd/52*12:.0f} months):")
        print(f"    P5:  ${np.percentile(final_pnl, 5):+.2f}")
        print(f"    P50: ${np.percentile(final_pnl, 50):+.2f}")
        print(f"    P95: ${np.percentile(final_pnl, 95):+.2f}")
        print(f"    P(net-negative): {p_negative:.1f}%")
        print(f"    P(down > 5% of capital): {p_down_5:.1f}%")


def rolling_winrate(trades: list[dict], window: int = 30) -> None:
    print(f"\n=== ROLLING {window}-TRADE WIN RATE ===")
    sorted_t = sorted(trades, key=lambda t: t["exit_time"])
    wins = [1 if t["pnl_usd"] > 0 else 0 for t in sorted_t]
    pnls = [t["pnl_usd"] for t in sorted_t]
    n = len(wins)
    low = (1.0, -1, "")
    high = (0.0, -1, "")
    for i in range(window, n + 1):
        wr = sum(wins[i - window:i]) / window
        end_date = sorted_t[i - 1]["exit_time"][:10]
        if wr < low[0]:
            low = (wr, i, end_date)
        if wr > high[0]:
            high = (wr, i, end_date)
    print(f"Lowest {window}-trade win rate: {100*low[0]:.1f}% (around {low[2]})")
    print(f"Highest {window}-trade win rate: {100*high[0]:.1f}% (around {high[2]})")


def main() -> None:
    path = RESULTS_PATH if len(sys.argv) < 2 else Path(sys.argv[1])
    trades = load_trades(path)
    print(f"Loaded {len(trades)} trades from {path.name}")

    yearly_breakdown(trades)
    ticker_breakdown(trades)
    exit_reason_breakdown(trades)
    monte_carlo(trades)
    forward_bootstrap(trades)
    rolling_winrate(trades)


if __name__ == "__main__":
    main()

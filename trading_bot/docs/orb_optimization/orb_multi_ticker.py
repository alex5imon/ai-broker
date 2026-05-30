"""Phase 3: multi-ticker test of the IS/OOS-validated 30min/R2 variant.

Phase 1 found `range=15min + target=close` as the lookback winner.
Phase 2 IS/OOS revealed that variant degrades materially OOS (return
ratio 0.30) — partly lookback-fit. The most robust variant was
**30min/R2** (OOS PF actually > IS PF, return ratio 0.77, OOS DD 3.89%).

This phase runs 30min/R2 on the 13-ETF universe (SPY + QQQ + 11 SPDR
sectors) to check whether the edge generalizes beyond SPY or is
SPY-specific.

Decision criteria (informal):
  - % of tickers with PF > 1.10 (we want most of the basket)
  - Median OOS PF across tickers (> 1.10 desired)
  - No ticker with catastrophic loss (> 15% DD)
  - Per-ticker IS/OOS PF consistency

Cheap to run (~3 min for 13 tickers).
"""

from __future__ import annotations

import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from trading_bot.docs.orb_optimization.orb_sweep import (
    Cfg,
    _list_trading_days,
    run_backtest,
)


UNIVERSE: tuple[str, ...] = (
    "SPY", "QQQ", "XLF", "XLK", "XLE", "XLV", "XLI", "XLY",
    "XLP", "XLU", "XLB", "XLRE", "XLC",
)

IS_END: date = date(2023, 8, 14)

# The variant under test
CFG = Cfg(range_minutes=30, target_r=2.0, label="30min/R2")


def split_days(days: list[date]) -> tuple[list[date], list[date]]:
    return [d for d in days if d <= IS_END], [d for d in days if d > IS_END]


def main() -> int:
    print(f"Strategy under test: {CFG.label}")
    print(f"Per-ticker IS/OOS — IS end {IS_END.isoformat()}")
    print()
    print(
        f"{'Ticker':>6s}  {'Days':>5s}  "
        f"{'IS Trd':>6s} {'IS PF':>6s} {'IS Ret%':>8s} {'IS DD%':>7s}  "
        f"{'OOS Trd':>7s} {'OOS PF':>7s} {'OOS Ret%':>9s} {'OOS DD%':>8s}  "
        f"{'OOS/IS PF':>10s}",
    )
    print("-" * 130)

    is_pfs: list[float] = []
    oos_pfs: list[float] = []
    oos_returns: list[float] = []
    oos_dds: list[float] = []
    pf_robust_count: int = 0  # OOS PF >= 1.10
    pf_above_unity_count: int = 0  # OOS PF >= 1.0

    rows: list[tuple] = []
    for t in UNIVERSE:
        days = _list_trading_days(t)
        if not days:
            print(f"{t:>6s}  NO DATA")
            continue
        is_days, oos_days = split_days(days)
        if not is_days or not oos_days:
            print(f"{t:>6s}  insufficient split")
            continue
        is_cfg = Cfg(**{**CFG.__dict__, "label": f"{t}_IS"})
        oos_cfg = Cfg(**{**CFG.__dict__, "label": f"{t}_OOS"})
        r_is = run_backtest(is_cfg, is_days, ticker=t)
        r_oos = run_backtest(oos_cfg, oos_days, ticker=t)

        is_pf = r_is.profit_factor
        oos_pf = r_oos.profit_factor
        pf_ratio = oos_pf / is_pf if is_pf > 0 else 0
        print(
            f"{t:>6s}  {len(days):>5d}  "
            f"{r_is.num_trades:>6d} {is_pf:>6.3f} {r_is.return_pct:>+8.2f} {r_is.max_drawdown_pct:>7.2f}  "
            f"{r_oos.num_trades:>7d} {oos_pf:>7.3f} {r_oos.return_pct:>+9.2f} {r_oos.max_drawdown_pct:>8.2f}  "
            f"{pf_ratio:>10.2f}",
        )
        rows.append((t, is_pf, oos_pf, r_oos.return_pct, r_oos.max_drawdown_pct, pf_ratio))
        is_pfs.append(is_pf)
        oos_pfs.append(oos_pf)
        oos_returns.append(r_oos.return_pct)
        oos_dds.append(r_oos.max_drawdown_pct)
        if oos_pf >= 1.10:
            pf_robust_count += 1
        if oos_pf >= 1.00:
            pf_above_unity_count += 1

    n = len(rows)
    if n == 0:
        print("No tickers processed.", file=sys.stderr)
        return 1

    print("-" * 130)
    print()
    print("=== Aggregate stats (across all tickers tested) ===")
    print(f"  Tickers tested:                {n}")
    print(f"  OOS PF median:                 {statistics.median(oos_pfs):.3f}")
    print(f"  OOS PF mean:                   {statistics.mean(oos_pfs):.3f}")
    print(f"  OOS PF range:                  [{min(oos_pfs):.3f}, {max(oos_pfs):.3f}]")
    print(f"  OOS Return median:             {statistics.median(oos_returns):+.2f}%")
    print(f"  OOS Return mean:               {statistics.mean(oos_returns):+.2f}%")
    print(f"  OOS DD median:                 {statistics.median(oos_dds):.2f}%")
    print(f"  OOS DD max:                    {max(oos_dds):.2f}%")
    print(
        f"  Tickers with OOS PF >= 1.10:   "
        f"{pf_robust_count} / {n} ({pf_robust_count / n * 100:.0f}%)",
    )
    print(
        f"  Tickers with OOS PF >= 1.00:   "
        f"{pf_above_unity_count} / {n} ({pf_above_unity_count / n * 100:.0f}%)",
    )
    print(
        f"  Tickers with OOS PF >= 0.85 × IS PF: "
        f"{sum(1 for r in rows if r[5] >= 0.85)} / {n}",
    )

    print()
    print("=== Decision lens ===")
    median_ok = statistics.median(oos_pfs) >= 1.10
    breadth_ok = pf_robust_count / n >= 0.70
    consistency_ok = (sum(1 for r in rows if r[5] >= 0.85) / n) >= 0.70
    print(f"  Median OOS PF >= 1.10?          {'YES' if median_ok else 'no'}")
    print(f"  >= 70% tickers with PF >= 1.10? {'YES' if breadth_ok else 'no'}")
    print(f"  >= 70% tickers with PF ratio >= 0.85? {'YES' if consistency_ok else 'no'}")
    if median_ok and breadth_ok and consistency_ok:
        print("  → PROCEED to walkforward A/B with regime-matched harness")
    else:
        print("  → INSUFFICIENT — SPY-specific edge; do not promote to multi-ticker sleeve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

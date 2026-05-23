"""Phase 2: in-sample / out-of-sample validation of Phase 1's best variants.

Phase 1 (orb_sweep.py) found `range=15min + target=close` lifted SPY
return from +17.83% (baseline) to +68.52% over 5.7 years. Phase 2 splits
the period into IS (in-sample) and OOS (out-of-sample) to check that the
edge isn't lookback-fit.

Split:
  IS  = 2020-08-14 → 2023-08-14 (~3 years)
  OOS = 2023-08-15 → 2026-05-08 (~2.7 years)

For each candidate variant, report IS and OOS metrics side-by-side and
the OOS/IS ratio for return and PF. A variant that fits the data will
show IS strongly positive and OOS materially worse; a real edge holds
similar PF/return across both.

Decision bar (informal): OOS PF >= 0.85 × IS PF AND OOS return positive.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Reuse the Phase 1 backtester rather than duplicating code
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from trading_bot.docs.orb_optimization.orb_sweep import (
    Cfg,
    Result,
    _list_trading_days,
    run_backtest,
    fmt_row,
)
from datetime import time


IS_END: date = date(2023, 8, 14)


def split_days(days: list[date]) -> tuple[list[date], list[date]]:
    is_days = [d for d in days if d <= IS_END]
    oos_days = [d for d in days if d > IS_END]
    return is_days, oos_days


def compare(cfg: Cfg, is_days: list[date], oos_days: list[date]) -> None:
    r_is = run_backtest(Cfg(**{**cfg.__dict__, "label": cfg.label + " [IS]"}), is_days)
    r_oos = run_backtest(Cfg(**{**cfg.__dict__, "label": cfg.label + " [OOS]"}), oos_days)
    print("  " + fmt_row(r_is))
    print("  " + fmt_row(r_oos))
    pf_ratio = r_oos.profit_factor / r_is.profit_factor if r_is.profit_factor > 0 else 0
    ret_ratio = r_oos.return_pct / r_is.return_pct if r_is.return_pct != 0 else 0
    held = "PASS" if r_oos.profit_factor >= 0.85 * r_is.profit_factor and r_oos.return_pct > 0 else "FAIL"
    print(
        f"  → OOS/IS:  PF ratio={pf_ratio:>5.2f}  return ratio={ret_ratio:>5.2f}  "
        f"verdict={held}\n",
    )


def main() -> int:
    days = _list_trading_days()
    if not days:
        print("No SPY cache", file=sys.stderr)
        return 1

    is_days, oos_days = split_days(days)
    print(f"IS:  {is_days[0]} → {is_days[-1]} ({len(is_days)} days)")
    print(f"OOS: {oos_days[0]} → {oos_days[-1]} ({len(oos_days)} days)\n")

    candidates = [
        Cfg(range_minutes=5, target_r=1.0, label="5min/R1 (baseline)"),
        Cfg(range_minutes=5, target_r=3.0, label="5min/R3"),
        Cfg(range_minutes=5, target_r=None, label="5min/close"),
        Cfg(range_minutes=15, target_r=2.0, label="15min/R2"),
        Cfg(range_minutes=15, target_r=None, label="15min/close"),
        Cfg(range_minutes=30, target_r=2.0, label="30min/R2"),
        Cfg(range_minutes=30, target_r=None, label="30min/close"),
        Cfg(range_minutes=15, target_r=None, min_range_pct=0.002, label="15min/close/min0.2%"),
        Cfg(range_minutes=15, target_r=None, entry_cutoff_et=time(12, 0), label="15min/close/cutoff12"),
    ]

    for c in candidates:
        compare(c, is_days, oos_days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

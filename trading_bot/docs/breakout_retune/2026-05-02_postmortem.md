# Breakout Retune — Postmortem & A/B (2026-05-02)

Tracking issue: TODO A in [`return_improvement_todos`](../../../return_improvement_todos.md).

## Why this exists

`breakout` was disabled in [PR #12](https://github.com/alex5imon/ai-broker/pull/12) on 2026-04-28 after the 13-ETF 2020-2026 walkforward showed PF 0.292, WR 11.5%, OOS Return -17.0%. The 2008-2021 SPY-only walkforward had given a PF 2.385 read; that turned out to be a regime-favorable artifact.

The acceptance bar for re-enabling: PF 95% CI lower bound > 1.0 individually on the 13-ETF 2020-2026 walkforward, AND portfolio CI does not regress vs current `[1.014, 1.190]`. Anything weaker = stays disabled.

## Step 0 — Diagnostic postmortem

Single-window simple backtest with `breakout` solo, `max_positions=13`, $1000 alloc, no regime filter on 13 ETFs from 2020-07-27 to 2026-04-16.

**Result: 20 trades, 0 wins, -18.0% return, 17 stop-outs.**

### Failure mode

Every entry that triggered ended up at stop-loss (or open at backtest end). Same-day fakeouts (4) plus slow-grind-to-stop (14) plus 3 still-open. Median hold 348h. The strategy is buying 5-min bars that print the daily 20-day high — by definition, the local top.

| Year | Trades | Win% | Total P&L |
|---|---:|---:|---:|
| 2021 | 1 | 0% | -$12.11 |
| 2022 | 1 | 0% | -$12.08 |
| 2024 | 2 | 0% | -$24.82 |
| 2025 | 10 | 0% | -$98.99 |
| 2026 | 6 | 0% | -$31.95 |

50% of failures concentrated in 2025 — the late-2025 selloff fired the strategy hardest and failed worst (every "20-day high" was a top tick).

### Per-ticker

| Ticker | N | Win% | P&L |
|---|---:|---:|---:|
| XLY | 4 | 0% | -$50.11 |
| XLF | 4 | 0% | -$45.61 |
| XLRE | 3 | 0% | -$24.26 |
| XLV | 2 | 0% | -$17.05 |
| XLP | 2 | 0% | -$20.62 |
| XLC, XLK | 2 each | 0% | small |
| XLI | 1 | 0% | -$11.16 |

### Time-of-day

Entries spread across 09:30 (4), 10:00-13:30 (11), 14:00-15:30 (5). No clustering — time-of-day filter alone won't rescue.

### Structural problem

Daily-20-day-high signal × 5-min execution = systematic top-ticking. The first 5-min bar that prints the daily high IS the local high, and the volume confirmation requirement ensures we fire at exhaustion. Combined with a 10-day Donchian exit that stays inactive while price grinds in a tight range below entry, and a 3% stop right at ETF noise level, every entry is a guaranteed loser.

## Step 1 — Hypotheses retained after diagnostic

Original H1 (Donchian 20→10), H2 (time-of-day filter), H3 (volume mult tighter) **dropped** — the data shows they don't address the structural pathology.

**Track 1** — incremental filter stack on the existing daily-high signal:
- H4: per-ticker trend filter (`price > own 50-day SMA`)
- H5: ATR expansion (`current ATR > 1.2× mean(ATR_14, last 20)`)
- H6: pullback entry (wait for retest of breakout level rather than buying the bar that breaks it)

**Track 2** — architecture pivot to Opening Range Breakout (ORB):
- 09:30-10:00 ET defines `[orb_low, orb_high]` from first 6 5-min bars
- Entry: break of `orb_high` before 11:30 ET cutoff with volume confirm
- Stop: `orb_low`; Target: `orb_high + R × range` (R=1.0 default)
- INTRADAY hold — backtester force-closes at 15:50 ET wind-down
- Variants: baseline (R=1.0, 6-bar range), R=2.0, 3-bar range, strict 2× volume

## Step 2 — A/B simple-backtest matrix

(13-ETF, 2020-07-27 → 2026-04-16, no regime filter, max_positions=13, $1000 alloc each.)

_Results pending — backtests in flight._

| Variant | Trades | Win% | PF | Total P&L | Return | MaxDD | Sharpe |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Track 1: filters on existing breakout signal** |
| baseline (no filter)  | 20 | 0.0% | 0.00 | -$179.95 | -18.00% | -19.0% | 2.14 |
| +H4 trend | _TBD_ | | | | | | |
| +H5 ATR-expansion | _TBD_ | | | | | | |
| +H6 pullback | _TBD_ | | | | | | |
| +H4+H5+H6 stacked | _TBD_ | | | | | | |
| **Track 2: Opening Range Breakout** |
| ORB baseline (R=1.0, 6-bar) | _TBD_ | | | | | | |
| ORB R=2.0 | _TBD_ | | | | | | |
| ORB 30-min range (3-bar) | _TBD_ | | | | | | |
| ORB strict vol (2.0×) | _TBD_ | | | | | | |

## Step 3 — Walkforward (survivors only)

A variant survives Step 2 iff (a) trade count ≥ 30 AND (b) PF > 1.10. Walkforward each survivor on 6 yearly windows with 1000-resample bootstrap CI. Acceptance: PF 95% CI lower bound > 1.0 individually.

_Pending Step 2 results._

## Step 4 — Portfolio walkforward

If any variant clears Step 3, re-enable in `multi_strategy.strategies` at \$1000 allocation (target uniform per-sleeve in paper-trading phase) with mean_reversion and overnight_drift each at \$1000 (idle \$2000 until TF/sentiment retunes). Run portfolio walkforward — must hold or improve current portfolio CI `[1.014, 1.190]`.

_Pending Step 3._

## Step 5 — Decision

_Pending all steps._

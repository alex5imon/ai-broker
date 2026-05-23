# Cross-sectional momentum — A/B walkforward report

**Date**: 2026-05-23
**Ticket**: [#44](https://github.com/alex5imon/ai-broker/issues/44)
**Strategy file**: `trading_bot/strategy/strategies/cross_sectional_momentum.py`
**Walkforward log**: `backtest_results/walkforward_2020-07-27_to_2026-04-16_20260523T215604.json`

## Setup

Regime-matched walkforward, per ticket spec:

```
python -m trading_bot.multi_strategy_backtest \
    --from 2020-07-27 --to 2026-04-16 \
    --config /tmp/config_csm_test.yaml \
    --multi-intraday \
    --tickers SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC \
    --strategies cross_sectional_momentum \
    --cash 2500 \
    --walkforward --wf-window 365 --wf-step 365
```

6 non-overlapping yearly windows. Regime filter on (SPY > 50-day SMA).

**Strategy params** (deviates from ticket spec for cache-fit reasons):

| Param | Ticket spec | A/B run | Reason |
|---|---|---|---|
| `lookback_days` | 126 | **60** | Daily cache populated with 120-day lookback (see `data/cache/XLF/2026-04-16_daily.parquet` = 82 rows). 60-day momentum is a standard variant. |
| `skip_recent_days` | 21 | **0** | Same constraint — 126+21=147 bars > 120 available. |
| `top_n` | 3 | 3 | Per spec. |
| `disaster_stop_pct` | 0.15 | 0.15 | Per spec. |
| `rebalance_time_et` | 09:35 | 09:35 | Per spec (`run_multi_ticker_intraday` warmup gate of 30 bars skips Day-1 09:35 but subsequent rebalance days fire normally). |

**To redo with the full 126/21 lookback**, populate the daily cache with a deeper history (`alpaca_downloader` `lookback_days` is currently hard-coded to 120 — would need to extend the request range or post-process the parquet files). Out of scope for this A/B.

## Results

### Aggregate (60 trades pooled across 6 windows)

| Metric | Value |
|---|---|
| Trades | 60 |
| Wins / Losses | 32 / 28 |
| Win rate | **53.33%** |
| Profit factor | **2.334** |
| Total P&L | +$903.19 |
| OOS Return | **+39.08%** (on $2500 cash) |
| Sharpe (per-trade approx) | 0.273 |

### Bootstrap 95% CIs

| Metric | Point | CI 95% |
|---|---|---|
| profit_factor | 2.334 | **[1.123, 4.631]** ✓ stat-significant > 1.00 |
| win_rate | 0.533 | [0.417, 0.667] |
| mean_return | 0.026 | [0.003, 0.053] |
| sharpe_approx | 0.273 | [0.041, 0.474] — lower bound positive, point below 0.40 bar |

### Per-window

| Window | Trades | WR% | PF | Return% | MaxDD% |
|---|---:|---:|---:|---:|---:|
| 2020-07-27 → 2021-07-26 | 12 | 75.0 | 4.794 | +16.08 | 5.05 |
| 2021-07-27 → 2022-07-26 | 8 | 0.0 | 0.000 | -10.91 | 13.57 |
| 2022-07-27 → 2023-07-26 | 12 | 58.3 | 3.320 | +8.03 | 4.98 |
| 2023-07-27 → 2024-07-25 | 12 | 50.0 | 1.741 | +2.51 | 4.75 |
| 2024-07-26 → 2025-07-25 | 9 | 66.7 | 16.814 | +11.42 | 4.92 |
| 2025-07-26 → 2026-04-16 | 7 | 57.1 | 5.328 | +8.99 | 3.58 |

**Positive OOS windows: 5/6.** Only the 2022 bear-market window negative.

## Acceptance scorecard (per #44)

| Criterion | Bar | Actual | Pass |
|---|---|---|---|
| Portfolio PF | ≥ 1.10 | 2.334 | ✅ |
| PF 95% CI lower bound | ≥ 1.00 | 1.123 | ✅ |
| OOS Return positive | ≥ 4 of 6 windows | 5 of 6 | ✅ |
| Sharpe (point) | ≥ 0.40 | 0.273 | ❌ |
| Sharpe CI lower bound | > 0 | 0.041 | ✅ (positive but point below bar) |
| Max drawdown | ≤ 15% | 13.57% (worst window) | ✅ |

**5 of 6 sub-criteria pass; 1 hard miss (Sharpe point estimate below 0.40 bar).**

## Decision lens

**For enabling**:
- PF 2.334 with CI lower bound 1.12 — far above any prior shelved strategy
- 5-of-6 OOS positive windows, including the high-vol 2024 regime
- Max DD well under cap
- Genuinely uncorrelated with mean_reversion (intraday RSI dip-buy) and overnight_drift (close-to-open premium) — adds a new return source
- 60-trade sample is small; per-trade Sharpe is noisy on small samples. PF + win rate + DD are more stable

**For keeping disabled**:
- Strict reading of ticket #44 acceptance bar — Sharpe 0.40 is a hard line
- 2022 window: 8 trades, 0 wins — momentum collapses in fast bear markets, as expected. -10.91% / 13.57% DD is uncomfortable for a 4-month period
- Sample distorted by cache constraint: only ~10 trades/window because the first ~5 months of each window are skipped while the daily lookback builds up; first rebalance within each window fires ~Dec/Nov, missing 7+ months of potential rebalances
- Ticket spec lookback (126/21) wasn't actually tested — 60-day was used to fit the cache. A different lookback may push Sharpe up or down

## Recommendation

**Hold at `enabled: false`** until one of:

1. Cache is extended to support the full ticket-spec 126/21 lookback and the A/B is re-run with those params. If Sharpe still misses, accept the strict criteria and shelve the strategy.
2. A scope decision is made to relax the Sharpe bar for low-turnover monthly strategies (the existing overnight_drift was enabled with PF 1.052 CI [0.964, 1.195] — below the strict 1.10 bar — when other metrics supported it, so there's precedent for case-by-case judgment).

Either way, the strategy plumbing (loader injection, backtester SWING-honor patch, ticker-in-position-dict fix) lands as it has independent value for #45 (pair stat-arb) and any future cross-sectional sleeve. The strategy module itself stays in tree as disabled, ready to enable when the data and acceptance criteria align.

## Update — 2026-05-23 evening: ticket-spec lookback A/B (126/21)

After deepening the daily cache to 200 rows per file (via the new
`alpaca_downloader --daily-lookback 200` flag) and re-running with the
ticket-spec params, results were **substantially worse** than the 60/0
variant:

| Criterion | Bar | 60/0 | 126/21 (ticket spec) |
|---|---|---:|---:|
| Trades | — | 60 | 45 |
| Portfolio PF | ≥ 1.10 | **2.334** ✅ | **0.915** ❌ |
| PF CI lower bound | ≥ 1.00 | 1.123 ✅ | 0.389 ❌ |
| OOS Return | — | +39.08% | **-7.44%** |
| Positive OOS windows | ≥ 4 of 6 | 5/6 ✅ | **2/6** ❌ |
| Max drawdown | ≤ 15% | 13.57% ✅ | 15.45% ❌ (marginal) |
| Sharpe (point) | ≥ 0.40 | 0.273 ❌ | -0.033 ❌ |
| Sharpe CI lower | > 0 | 0.041 ✅ | -0.347 ❌ |

Per-window for 126/21:

| Window | Trades | WR% | PF | Return% | MaxDD% |
|---|---:|---:|---:|---:|---:|
| 2020-07-27 → 2021-07-26 | 7 | 85.7 | 326.582 | +6.80 | 5.36 |
| 2021-07-27 → 2022-07-26 | 9 | 11.1 | 0.027 | -13.20 | 15.45 |
| 2022-07-27 → 2023-07-26 | 11 | 54.5 | 0.684 | -2.84 | 10.50 |
| 2023-07-27 → 2024-07-25 | 5 | 100.0 | — | +10.44 | 3.78 |
| 2024-07-26 → 2025-07-25 | 9 | 44.4 | 0.540 | -3.76 | 11.78 |
| 2025-07-26 → 2026-04-16 | 4 | 25.0 | 0.102 | -3.31 | 5.67 |

**Interpretation:** the ticket-spec parameters (126-day lookback,
21-day skip — Asness/Moskowitz convention) do not work on this
universe in this regime:

- The 21-day skip *removes* the very recent trend continuation that
  drives near-term sector rotation. Sector momentum is faster than
  single-name momentum, where the skip-month variant is well-supported.
- 126 days is too long a lookback for sector rotation that turns over
  in months, not quarters.
- 2022 bear market clobbered the strategy harder under 126/21 (11.1%
  WR, PF 0.027) — the longer-horizon signal anchored on stale bullish
  trends as the regime shifted.

The 60-day, no-skip variant captures faster sector momentum and was
the only configuration that cleared the PF / MaxDD / OOS-positive bars.

## Final disposition

The strategy is **shelved** under the literal ticket-spec parameters.
Two open paths:

1. **Re-spec the ticket** with the empirical 60/0 finding and decide
   whether 60/0 (which cleared PF, MaxDD, OOS-positive but missed
   Sharpe 0.40) is enough to enable. This is a scope decision on the
   Sharpe acceptance bar for low-turnover monthly strategies
   (precedent: overnight_drift enabled with PF CI lower bound 0.964 —
   below the strict 1.10 bar — based on other supporting metrics).

2. **Shelve and try a different signal.** Sector momentum may
   simply not have an exploitable edge with the structure ticket #44
   described. Alternative: top-N relative-strength rotation on a
   60-day window (basically the 60/0 variant under a different
   ticket), or move on to #45 / #46 / #48 / #138.

Either way, the plumbing and backtester fixes shipped in PR #155
have value beyond this one strategy.

## Artifacts

- Walkforward JSON: `backtest_results/walkforward_2020-07-27_to_2026-04-16_20260523T215604.json`
- Backtest log: `trading_bot/logs/multi_strategy_backtest.log`
- Cache files: `data/cache/{TICKER}/{YYYY-MM-DD}_daily.parquet` (1489 files × 13 tickers, written by `alpaca_downloader --full-daily-history` on 2026-05-23)

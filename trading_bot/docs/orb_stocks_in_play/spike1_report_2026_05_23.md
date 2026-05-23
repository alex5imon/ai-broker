# ORB-on-stocks-in-play — Spike 1 report (sample-rate sanity check)

**Date**: 2026-05-23
**Ticket**: [#138](https://github.com/alex5imon/ai-broker/issues/138)
**Script**: `trading_bot/docs/orb_stocks_in_play/spike1_sample_rate.py`

## Goal

Per ticket #138 Spike 1 (~1 day):

> Compute the relative opening-volume ratio for each ETF on each day:
> `first_5min_volume / mean(first_5min_volume, last 14 days)`.
> Count how many trades the universe filter alone would produce per week.
> **Decision gate:** if fewer than ~3 trades/week across the basket, the
> ETF version is sample-starved.

## Method

- **Universe**: 13 ETFs (SPY, QQQ, 11 SPDR sectors) — matches live bot watchlist
- **Data**: existing 1-min Alpaca cache (`data/cache/{TICKER}/{DATE}_intraday.parquet`)
- **first_5min_volume**: sum of the 5 1-min bars at 09:30 ET — 09:34 ET
- **Baseline**: rolling 14-day mean of previous first_5min_volume per ticker
- **Date range**: 2020-08-14 → 2026-05-08 (1,439 trading days)

Two variants counted:
- **Top-N**: top-2 tickers by ratio per day (matches paper's top-quintile-of-universe slice on our smaller basket).
- **Threshold**: any ticker with `ratio >= 1.5`.

## Results

| Variant | Total signal-days | Per week |
|---|---:|---:|
| Top-2 per day | 2,876 | **9.62** |
| Ratio ≥ 1.5 | 2,055 | **6.87** |

Both far exceed the **3 trades/week** sanity bar.

Threshold distribution per day: min 0, p50 1, mean 1.43, p90 4, max 13.

### Per-ticker hit rate (ratio ≥ 1.5)

| Ticker | Cached days w/ratio | Hits | Hit rate |
|---|---:|---:|---:|
| QQQ | 1,382 | 233 | 16.9% |
| SPY | 1,431 | 229 | 16.0% |
| XLB | 623 | 113 | 18.1% |
| XLC | 338 | 54 | 16.0% |
| XLE | 1,272 | 238 | 18.7% |
| XLF | 1,312 | 240 | 18.3% |
| XLI | 948 | 160 | 16.9% |
| XLK | 777 | 125 | 16.1% |
| XLP | 814 | 160 | 19.7% |
| XLRE | 435 | 91 | 20.9% |
| XLU | 903 | 162 | 17.9% |
| XLV | 906 | 158 | 17.4% |
| XLY | 475 | 92 | 19.4% |

Hit rates land in a tight 16–21% band across all 13 tickers. No single
ticker dominates the signal — exactly the diversified profile you want
for a basket strategy.

## Verdict

**PASS — proceed to Spike 2 (single-instrument feasibility).**

Sample rate is healthy enough that the ORB strategy on this ETF basket
will not be sample-starved. The volume filter is doing real work (~17%
of days hit the threshold per ticker; without the filter it would be
100% of days = no filter at all).

## Caveats to carry into Spike 2

1. **Sample rate ≠ profitability.** Spike 1 only proves trade volume.
   Spike 2 must validate the actual ORB signal (long on break above
   first-5-min high, short on break below, ATR-scaled stop, exit at
   close) actually has positive expectancy after the volume filter.
2. **17% per-ticker hit rate** is close to the paper's reported 17%
   win rate, but the paper's number was on the entry-trigger filter
   (volume + ORB break), not just the volume filter alone. Final win
   rate could be lower once ORB break is also required.
3. **Coverage gap**: XLC (352 days), XLRE (449 days), XLY (489 days),
   XLB (637 days) — cache is shallow for these newer SPDR sectors.
   Spike 2 should weight performance by coverage or restrict the
   universe to the 7 tickers with ≥ 800 days for the first pass.
4. **No bias correction yet.** The 14-day rolling mean is a naive
   baseline. Earnings cluster windows, FOMC days, OPEX, and quad-witch
   days all systematically have elevated opening volume — the paper
   doesn't deduct these and we don't either, but a meaningful fraction
   of "stocks in play" signals on Fed-day mornings won't translate to
   actual edge.

## Next step

Spike 2 (~1 day): build a minimum-viable ORB backtest using
`multi_strategy_backtest` infrastructure. Universe SPY only initially.
Compare against a "naive ORB" (no volume filter) to confirm the volume
filter is doing useful work.

If Spike 2 also passes → file production-integration issue.
If Spike 2 fails → document and close #138.

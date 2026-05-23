# ORB-on-stocks-in-play — Spike 2 report (single-instrument feasibility)

**Date**: 2026-05-23
**Ticket**: [#138](https://github.com/alex5imon/ai-broker/issues/138)
**Script**: `trading_bot/docs/orb_stocks_in_play/spike2_orb_backtest.py`
**Predecessor**: Spike 1 passed sample-rate check (9.62 top-2 signals/week)

## Goal

Per ticket #138 Spike 2:

> Build a minimum-viable ORB backtest. Universe: SPY only initially.
> Compare against a "naive ORB" (no volume filter) to confirm the
> volume filter is doing real work.

## Strategy under test

Same mechanics for both variants on SPY 1-min bars:

| Element | Value |
|---|---|
| Opening range | First 5 1-min bars (09:30 – 09:34 ET) |
| Entry | First bar from 09:35 ET to 15:30 ET cutoff whose high crosses OR_high |
| Fill | OR_high (conservative — assumes stop-order fill at the level) |
| Stop | OR_low |
| Target | OR_high + 1.0 × (OR_high - OR_low) (1R) |
| Wind-down | 15:55 ET — close at last bar |
| Sizing | 1% of equity per trade, capped at 95% of equity |
| Re-entry | None (one trade max per day) |

Variant difference:
- **Naive ORB**: always allowed to enter on a break.
- **Stocks-in-play ORB**: only enters when today's first-5-min volume
  ÷ mean(prior 14 trading days' first-5-min volume) ≥ 1.5.

## Setup

- Data: 1,431 SPY trading days (2020-08-14 → 2026-05-08), post 14-day warm-up
- In-play flag: 229 of 1,431 (16.0%) flagged in-play
- Starting equity: $2,500 (matched to existing sleeve allocation scale)

## Results

```
Naive ORB (SPY)              trades=1249  WR= 52.6%  PF= 1.142  Return= +16.23%  MaxDD= 5.29%
Stocks-in-play ORB (SPY)     trades= 190  WR= 50.0%  PF= 1.054  Return=  +1.13%  MaxDD= 3.46%
```

| Metric | Naive | In-play | Delta |
|---|---:|---:|---:|
| Trades | 1,249 | 190 | -1,059 |
| Win rate | 52.6% | 50.0% | **-2.6 pp** |
| PF | 1.142 | 1.054 | **-0.088** |
| Return | +16.23% | +1.13% | **-15.10 pp** |
| MaxDD | 5.29% | 3.46% | -1.83 pp |

## The smoking gun

Splitting the naive backtest's 1,249 trades by the in-play flag:

| Days | n | Wins | WR | Total P&L | Avg / trade |
|---|---:|---:|---:|---:|---:|
| **In-play** | 190 | 95 | 50.0% | $31.86 | **$0.17** |
| **Other** | 1,059 | 562 | 53.1% | $374.00 | **$0.35** |

The filter selects trades with **lower win rate** (50.0% vs 53.1%) and
**half the per-trade P&L** ($0.17 vs $0.35). This is the opposite of
what the paper found on individual stocks.

## Verdict

**KILL** — the "stocks in play" volume filter does not transfer from
individual stocks to ETFs. It is **anti-edge** on this universe.

The decision gate from Spike 2 ("In-play PF > naive PF AND in-play
return > naive return") fails on both criteria.

## Why (hypothesis)

The paper's mechanism: high opening volume on an individual stock
signals an idiosyncratic catalyst (earnings, news, upgrade) that
institutions are positioning into; the ORB break captures the
institutional follow-through.

On ETFs:

1. **No idiosyncratic catalyst.** ETF opening volume spikes are driven
   by macro/Fed/CPI prints, OPEX, quad-witch, and large basket
   rebalances — flows whose direction is often AGAINST the breakout
   side (e.g., a hot CPI print opens with heavy SPY selling that briefly
   rallies into the OR_high then reverses with the rest of the macro
   move). Breakouts on those days have weaker follow-through than on
   quiet days.

2. **Wider opening ranges.** High-volume opens produce wider ranges,
   which means larger stops, which means smaller positions for the same
   1% risk budget — capping winning trade $ size even when the breakout
   does work.

3. **Adverse selection on signal frequency.** The filter selects 16% of
   days. Those days are systematically the noisy ones; the remaining
   84% have cleaner breakout structure where the bid-offer dynamic
   actually supports continuation.

## Side finding worth noting

The **naive ORB** itself is mildly profitable on SPY: 1,249 trades,
52.6% WR, PF 1.142, +16.23% over 5.7 years (~2.9% annualized), MaxDD
5.29%. Not enough on its own to be a production sleeve (would need
to clear the project's regime-matched walkforward acceptance bar —
PF CI lower bound ≥ 1.00, etc.), but it suggests there is *some*
edge in the ORB structure on SPY that does not depend on the
volume filter.

This is **not in scope for #138** (which is specifically about the
stocks-in-play variant) but could justify a separate follow-up to
A/B the existing `OpeningRangeBreakoutStrategy` (which uses a
DIFFERENT intraday-relative volume filter) against a no-filter
variant.

## Disposition

**Close #138.** Spike 2 fails the decision gate. The ETF version
of stocks-in-play ORB does not work and does not warrant Spike 3
(production-integration A/B).

If the strategy is revisited later, it should be on a stocks
universe (NVDA / TSLA / COIN / individual mega-caps), not on ETFs
— matching the paper's domain.

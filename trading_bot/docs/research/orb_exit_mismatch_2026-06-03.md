# ORB "death by a thousand cuts" — exit-logic mismatch (research, advisory)

**Date:** 2026-06-03
**Sleeve:** `opening_range_breakout` (ORB)
**Status:** Advisory only. No `config.yaml` or strategy logic was changed by this analysis. Apply patches by hand after the validation gate below.

---

## TL;DR

**Verdict: SHELVE ORB (disable in paper).** Two independent failures:

1. **The deployed sleeve is mis-built** — a live-vs-backtest exit mismatch (see
   §2) collapses its win rate from 53% to 29% and inverts payoff to 0.72,
   producing the −$2.48 "thousand cuts" bleed. This is a real bug.
2. **But fixing it doesn't rescue ORB.** The recent regime-matched walkforward
   (2023-05→2026-05) **fails the gate even under the favourable let-winners-run
   exits**: PF 0.907, OOS −1.37%, Sharpe −0.036, PF 95% CI [0.648, 1.231]. The
   +23.71% in the 5.7-year in-sample was front-loaded in 2020–2022; the edge has
   **decayed out of the current regime**.

So the exit mismatch explains why live is *worse than flat*, but the strategy has
no durable edge to recover. Retuning exits would move ORB from bleeding to
roughly breakeven-to-negative — not worth the complexity when overnight_drift
carries the book. **Disable ORB**; keep the exit-mismatch finding (and the
harness fix in §5) as generic lessons. Same regime-decay trap that shelved
`breakout` and `trend_following`.

---

## 1. The symptom, quantified

Per-strategy economics, live settled trades since 2026-05-01 (`void_no_fill` /
`unresolved_exit` excluded):

| Sleeve | Trades | Win rate | Payoff actual | Payoff needed | Expectancy | Net |
|---|---:|---:|---:|---:|---:|---:|
| overnight_drift | 46 | 52% | 1.11 | 0.92 | +$0.37 | **+$17.07** |
| mean_reversion | 19 | 32% | 2.48 | 2.17 | +$0.05 | +$1.01 |
| **opening_range_breakout** | **14** | **29%** | **0.72** | **2.50** | **−$0.18** | **−$2.48** |

Two of three sleeves clear their breakeven-payoff bar. ORB does not — its
winners (0.72×) are *smaller* than its losers, at a 29% hit rate. That asymmetry
is the literal mechanism of "a thousand small cuts." overnight_drift is the
profit engine; ORB is the leak.

Live ORB exit distribution (since 2026-05-01): only **2 of 14 trades (14%)**
reached the 2R `take_profit`; the two full `stop_loss` hits (−$1.75) dominate the
loss column, while winners exit small via `time_stop`/`wind_down`.

> Caveat: 14 live trades is a small sample (2 stops drive most of the loss).
> Treated as a signal that motivated the backtest below, not a standalone verdict.

## 2. Root cause — live and backtest run different exit engines

`run_multi_ticker_intraday` in `multi_strategy_backtest.py` (and every other
backtest mode) **overrides each strategy's own stop/target** with ATR-based
exits (`_atr_adjusted_stops`: stop 2×ATR, target 5×ATR, trailing 2.5×ATR) plus
hardcoded 2%/40% sizing. It keeps only the strategy's *entry* signal and the
binary "let winners run" flag (`decision.target_price is None`).

`opening_range_breakout.py` (live) sets a **fixed** `target_price = orb_high +
target_r_multiple × range` (2R) and `trail_pct=None`. `evaluate_exit` only fires
on stop or target; otherwise the position waits for the intraday wind-down.

Net effect:

| | Backtest (validated) | Live (deployed) |
|---|---|---|
| Stop | 2×ATR | range low (~1R) |
| Target | 5×ATR (wide) | **fixed 2R** |
| Trailing stop | 2.5×ATR (yes) | **none** |
| Typical winner exit | ride to `eod_close` (avg hold 387 min) | cut at `time_stop`/`wind_down` |
| Win rate | **53.4%** | **29%** |

The validated edge depends on letting winners run to the close. Live throws that
away. **That is the bug.**

## 3. Evidence — ORB as validated

Standalone backtest, 13-ETF live universe, 2020-07-27 → 2026-05-08 (1,506
trading days), let-winners-run (ATR) exits:

```
Trades 2906 | Win% 53.4 | P&L +$237.09 (+23.71%) | MaxDD -10.9% | PF 1.09 | Sharpe 0.52 | AvgHold 387m
avg_win 0.492% | avg_loss -0.521% | payoff 0.94 (breakeven 0.87 at 53.4% WR)
```

Best trades exit `eod_close` (winners ran); worst exit `stop_loss`.

Structured evaluation (`backtest-expert/evaluate_backtest.py`): **69/100 —
"Refine"**, no red flags. Perfect Sample-Size (2,906) and Execution-Realism
(slippage modelled); weak only on Expectancy (thin PF). i.e. a genuine,
multi-regime, large-sample edge that is **marginal, not a star** — consistent
with the prior #156 walkforward (PF 1.176, CI [1.056, 1.309]).

### Walkforward (gate)

3-year regime-matched walkforward (2023-05-08 → 2026-05-08), 13 ETFs, 90-day
windows / 90-day step, 1,000-sample bootstrap CI, let-winners-run (ATR) exits:

```
Windows 12 | Trades 291 | WinRate 53.95% | PF 0.907 | OOS Return -1.37% | Sharpe -0.036
profit_factor 95% CI [0.648, 1.231]  (point 0.907)
win_rate      95% CI [0.478, 0.595]  (point 0.540)
mean_return   95% CI [-0.001, 0.000] (point -0.000)
sharpe_approx 95% CI [-0.155, 0.077] (point -0.036)
```

**This fails the gate.** PF point estimate is below 1.0, OOS return and Sharpe
are negative, and the PF CI lower bound (0.648) is far under the 1.0 the weekly
walkforward health-check requires. The win rate holds (~54%), so entries still
"work" mechanically — but the recent-regime payoff no longer covers costs. The
strong 5.7-year in-sample number (+23.71%, PF 1.09) was front-loaded in
2020–2022 and does **not** persist out-of-sample in the current regime. Classic
long-history false read (cf. `memory/feedback_regime_matched_walkforward.md`).

## 4. Recommendation — SHELVE

1. **Disable ORB in paper** (`strategies.opening_range_breakout.enabled: false`).
   The recent regime-matched walkforward fails even under the favourable
   let-winners-run exits, so there is no durable edge to deploy.
2. **Do not invest in the exit retune** as a live change. It is necessary to make
   ORB *coherent* but not sufficient to make it *profitable* — best case it
   reaches ~breakeven (PF 0.907) in the current regime.
3. **Re-evaluate only on a regime shift.** ORB had an edge in 2020–2022; if
   volatility/trend conditions return, re-run this walkforward before
   re-enabling. The entry logic is sound; the regime isn't.

### Advisory patch (apply by hand)

**`config.yaml` → `strategies.opening_range_breakout`**:
```yaml
      enabled: false   # shelved 2026-06-03: recent walkforward PF 0.907 (CI low 0.648),
                       # OOS -1.37%, Sharpe -0.036 — no edge in current regime.
                       # See trading_bot/docs/research/orb_exit_mismatch_2026-06-03.md
```

This leaves the live book on the two sleeves that *do* clear their bar:
overnight_drift (the engine) and mean_reversion (marginal-positive). It also ends
the ORB cut-stream that motivated this investigation.

## 5. Harness limitation (generic follow-up, not ORB-specific anymore)

Independent of the shelve decision, this investigation surfaced a real harness
gap worth fixing: the backtester **cannot test any strategy's precise live exit
config** — it ATR-overrides every strategy's stops/targets, validating only the
*direction* (let winners run) not a specific `trail_pct`/stop choice. This is
almost certainly why ORB's exit mismatch went unnoticed when it was first
enabled, and it is a latent risk for every future sleeve tune.

**Recommended follow-up:** add a "honor strategy-provided stops/targets/trail"
mode to `run_multi_ticker_intraday` (use `decision.stop_price` /
`decision.target_price` / `decision.trail_pct` when present, fall back to ATR
otherwise). That lets us A/B the *actual* live exit parameters before flipping
config — and closes a latent risk for every future sleeve tune.

Until that exists: mirror the validated ATR profile as closely as possible in
the patch above and rely on the paper-confirm window as the live gate.

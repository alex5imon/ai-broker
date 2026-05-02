# Trend-Following Retune — Postmortem & A/B (2026-05-02)

Tracking issue: [ai-broker#37](https://github.com/alex5imon/ai-broker/issues/37).

## Why this exists

`trend_following` was disabled in [PR #12](https://github.com/alex5imon/ai-broker/pull/12) on 2026-04-28 after the 13-ETF 2020-2026 walkforward showed PF 0.565, WR 29.2%, OOS Return -15.3%, PF 95% CI [0.30, 0.98]. Acceptance bar for re-enable: PF 95% CI lower bound > 1.0 individually on the 13-ETF 2020-2026 walkforward, AND portfolio CI does not regress vs current `[1.014, 1.190]`. Anything weaker = stays disabled.

Two parallel tracks were attempted; both failed.

## Step 0 — Diagnostic baseline

Single-window 13-ETF 2020-07-27 → 2026-04-16, trend_following solo, max_positions=13, no regime filter.

```
trades=63 win%=25.4 PF=0.37 return=-17.21% maxDD=18.28%
exit reasons: trailing_stop=57, stop_loss=3, backtest_end=3
```

Initial diagnosis (later proved wrong): "90% trail-stop dominance — exit logic is the prime suspect, not entry."

Per-year: 2026 alone = 33 trades, WR 21%, −$104. Per-ticker: uniformly negative (XLRE −$48 worst, XLE only ticker with a positive avg pnl ≥ 0). Same regime story as breakout.

## Engine quirk discovered

`MultiStrategyBacktester._atr_adjusted_stops` (multi_strategy_backtest.py:312) is hardcoded to compute `trail_pct = max(2.5×ATR/entry, 0.04)` and stamps that onto `trade.trail_pct`, ignoring the strategy's own `_atr_trail_mult` config and ignoring `decision.trail_pct`. The H2 trail-width sweep was a no-op for this reason.

A working-tree patch (added `StrategyDecision.disable_engine_trail`, made the multi-ticker entry path honor it) let the chandelier and Donchian variants actually run their own trail. With both variants shelved, the patch was reverted along with the rest of the experiment — no current strategy needs it. Worth re-introducing on the day a strategy genuinely wants to own its trail; the postmortem describes the diff.

## Track A — filter / exit-stack on existing 9/21 EMA signal

| Variant | Trades | WR% | PF | Return | MaxDD | Notes |
|---|---:|---:|---:|---:|---:|---|
| baseline (Step 0) | 63 | 25.4 | 0.37 | −17.21% | −18.28% | trail=57, stop=3 |
| H1 EMAs 20/50 | 51 | 23.5 | **0.27** | −15.80% | −16.21% | longer EMAs *worse* |
| H7 pullback entry | 63 | 27.0 | **0.40** | −13.28% | −14.84% | best Track A — still negative-EV |
| H10 chandelier k=3.0 | 17 | **0.0** | — | −15.76% | −16.67% | 14/17 stop_loss exits |
| H5 ToD 10:00–15:00 ET | 57 | 21.1 | 0.37 | −14.24% | −17.01% | filter neutral |
| H8 ADX(14)≥20 daily | 62 | 24.2 | 0.36 | −17.43% | −18.34% | filter near-neutral |
| H7+H8 stacked | 64 | 25.0 | 0.36 | −15.26% | −16.99% | stacking *worse* than H7 alone |

### What we learned about exit-quality vs entry-quality

H10 chandelier (loose trail) collapsed WR from 25% to **0%** while initial-stop exits jumped from 3 to 14. This was the diagnostic flip:

- The 25% "win rate" in the baseline came mostly from trail clipping briefly-positive trades at small losses
- Remove the trail mercy and the same trades ride straight to the 1.5×ATR initial stop
- **The signal generates negative-EV entries.** Trail tightness was masking, not causing, the underlying problem

H7 (pullback entry) — the best Track A variant — moved PF from 0.37 → 0.40. Real but not enough. H7+H8 stacking was *worse* than H7 alone (0.36) — the ADX filter dropped a few good trades while letting the bad ones through.

## Track B — Donchian-on-5min architecture pivot

`DonchianTrendStrategy`: 50-bar 5-min Donchian high entry (signal cadence aligned to execution cadence), daily SMA50 + ADX(14)≥20 trend gates, 10:00–15:00 ET window, strong-bar (close in upper third) requirement, 1.2× volume confirm, 1.5×ATR initial stop, chandelier exit (HH(22 5-min) − 3×ATR daily).

Baseline result (13-ETF, 2020-07-27 → 2026-04-16, max_positions=3):

```
trades=17 win%=0.0 PF=None return=-16.38% maxDD=18.03%
exit reasons: stop_loss=14, backtest_end=3
```

**Same convergent failure as H10 chandelier.** The Donchian-on-5min signal generates negative-EV entries, the chandelier is wider than the engine's hardcoded trail, and trades ride to the 1.5×ATR initial stop. 0% WR with 14 stop-outs.

## Track A walkforward (acceptance check)

H7 (best Track A survivor) 13-ETF 2020-07-27 → 2026-04-16, 23 × 90-day windows, 1000 bootstrap resamples.

```
Trades: 421  WinRate: 18.76%  PF: 0.260
OOS Return: -72.24%  Sharpe: -0.521
profit_factor CI 95%:  [0.184, 0.347]   point=0.260
win_rate CI 95%:       [0.152, 0.226]   point=0.188
mean_return CI 95%:    [-0.010, -0.007] point=-0.008
sharpe_approx CI 95%:  [-0.648, -0.408] point=-0.521
```

The PF 95% **upper bound** is 0.347 — the entire confidence interval sits below 1.0, not just the lower bound. The acceptance bar (PF CI lower bound > 1.0) is unreachable for this formulation regardless of further tuning.

Worse than the original PR #12 read (PF 0.565, CI [0.30, 0.98]) because Step 0 / H7 runs at `max_positions=13` (universe-blind), so we see the strategy at full firing rate; the original walkforward was at `max_positions=1`.

## Decision: SHELVE

Same logic as the breakout retune Phase 12. PF point estimates of 0.0–0.40 cannot have CI lower bound > 1.0 with any number of bootstrap resamples. The acceptance bar is unreachable.

## Code: nothing committed

All code from the experiment was reverted. The breakout retune (Phase 12) kept its opt-in flags in tree; this one does not, by design — carrying ~350 lines of dormant code on a sleeve we believe doesn't work imposes a real maintenance cost (readers have to figure out what's live vs dead) for the speculative case "we might revisit". The postmortem below is sufficient to reproduce any of the experiments.

What was tried in the working tree (and reverted):

| Lever | What it added | Cost to redo from this doc |
|---|---|---|
| H5 `time_of_day_filter` | US/Eastern entry-window guard in `evaluate_entry` | minutes |
| H7 `pullback_entry` | EMA-cross-with-retest + close-strength bar confirm | < 1 hour |
| H8 `require_adx` + `_compute_adx` | Wilder ADX(14) daily gate + helper | < 1 hour |
| H10 `chandelier_exit` | HH(N 5-min) − k×ATR daily trail in `evaluate_exit` | < 1 hour |
| `StrategyDecision.disable_engine_trail` | Field + multi-ticker engine branch to honor it | < 30 min |
| `DonchianTrendStrategy` | Full new class (50-bar 5-min Donchian + ADX + chandelier) | ~2 hours |

If a future revisit happens, redo whichever pieces it needs — the data above tells you which experiments are dead-on-arrival without re-running them.

Live `config.yaml` unchanged. `trend_following.enabled: false` remains.

## Lessons

1. **Engine overrides are silent killers.** A trail/stop knob on the strategy that the engine ignores produces a sweep that looks valid but tells you nothing. Always grep for whether the engine respects strategy params before designing a sweep.
2. **Trail is a diagnostic, not an exit fix.** A tight trail with low WR + lots of trail-exits looked like an exit problem; loosening the trail flipped to 0% WR + initial-stop exits. The trail wasn't causing the loss — it was disguising the entry-signal failure.
3. **Filter cascades on a weak base signal don't rescue.** Same lesson the breakout retune learned. H7+H8 was *worse* than H7 alone.
4. **Architecture pivot didn't help when the regime is structural.** Donchian-on-5min replaced the daily-signal × 5-min-execution mismatch from the EMA cross — same outcome (0% WR) — because the underlying problem is "broad ETFs in 2020-2026 don't intraday-trend long enough on a generic momentum signal".
5. **Convergent failure mode across tracks is informative.** When two architecturally different signals (EMA cross + Donchian-on-5min) produce the same 0% WR / 14 stop-outs profile, it is the regime, not the formulation.

## What was NOT tried (potential future iterations)

- Trend signals on individual high-momentum stocks (NVDA, TSLA, COIN). Mega-cap individual names trend more intraday than diversified ETFs (also the conclusion the breakout retune reached).
- Asymmetric R/R formulations: entries with wider initial stop AND wider chandelier would let signal noise-filter at the cost of larger losers. Was not tried because base WR was already 25%; doubling stop distance would push expected loss-per-trade past expected gain-per-trade.
- Daily-bar (not intraday) trend formulations on the 13-ETF basket. Phase 1-2 tested this and the daily mode was uniformly worse; not worth re-running.
- Sector-rotation / cross-sectional momentum across the 13 ETFs. Different strategy class entirely; would not be a "trend_following retune" but a new sleeve.

## References

- [PR #12](https://github.com/alex5imon/ai-broker/pull/12) — original disable decision
- [trading_bot/docs/breakout_retune/2026-05-02_postmortem.md](../breakout_retune/2026-05-02_postmortem.md) — convergent failure pattern
- `tune_history.md` Phase 13 (this attempt)
- `multi_strategy.md` — sleeve allocation table

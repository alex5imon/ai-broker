# Calendar overlay walkforward postmortem — all four overlays shelved

**Date:** 2026-05-03
**Issue:** [ai-broker#47](https://github.com/alex5imon/ai-broker/issues/47)
**Predecessor PR:** #50 (overlay infra + aggregate A/B)
**This PR:** postmortem; no config changes

## TL;DR

After the aggregate A/B (PR #50) shelved 3 of 4 overlays, `pre_long_weekend`
remained the only candidate to enable. The issue's acceptance criteria
also require a per-window OOS check ("≥ 4 of 6 yearly windows
positive"). Walkforward A/B run today shows `pre_long_weekend` **fails
that bar** (3 of 6 windows positive). Per the "do not iterate" rule,
all four overlays are now permanently shelved. Code in
`trading_bot/strategy/calendar_overlay.py` stays opt-in only,
consistent with the breakout shelved-try precedent (PR #43).

## Walkforward setup

* Window: 2020-07-27 → 2026-04-16 (~5.7 years)
* Universe: SPY, QQQ, XLF, XLK, XLE, XLV, XLI, XLY, XLP, XLU, XLB, XLRE, XLC
* Mode: `--multi-intraday --no-regime-filter` (daily cache sparse pre-2026)
* Walkforward: 6 non-overlapping yearly windows (`--wf-window 365 --wf-step 365`)
* Sleeve under test: `overnight_drift` (the only sleeve `pre_long_weekend` blocks)
* Variant config: `pre_long_weekend.enabled: true`, `min_weekend_days: 4` (true long weekends only)

## Per-window OOS results

| Window | Period | Baseline Return | + pre_long_weekend | Δ pp | Trades base / +overlay |
|---:|---|---:|---:|---:|---:|
| 0 | 2020-07-27 → 2021-07-26 | +6.63% | +5.98% | **−0.65** | 750 / 729 |
| 1 | 2021-07-27 → 2022-07-26 | −15.11% | −12.43% | **+2.69** | 753 / 729 |
| 2 | 2022-07-27 → 2023-07-26 | −12.45% | −12.75% | **−0.30** | 747 / 723 |
| 3 | 2023-07-27 → 2024-07-25 | +14.29% | +16.06% | **+1.76** | 747 / 726 |
| 4 | 2024-07-26 → 2025-07-25 | +3.46% | +3.40% | **−0.06** | 735 / 720 |
| 5 | 2025-07-26 → 2026-04-16 | +4.44% | +7.05% | **+2.61** | 540 / 528 |

Summary:
- **Positive windows: 3 / 6** — fails the ≥ 4 of 6 bar
- No catastrophic loss (worst window: −0.65 pp, well within the −2 pp tolerance)
- Aggregate Δ across 6 windows: **+6.05 pp** (consistent with the +6.30 pp aggregate A/B in PR #50)

## Why the walkforward disagreed with the aggregate A/B

The aggregate Sharpe lift (+0.08, PR #50) was real but concentrated in
two windows (W1 the 2022 bear market: +2.69 pp; W5 the 2025-2026
window: +2.61 pp). Three windows showed mild negative deltas.

The issue's acceptance bar was specifically designed to catch this
pattern — an overlay that wins big in 2 of 6 years but loses or breaks
even in the others is overfit to those years' calendar structure, not
a durable edge. Holding to the 4-of-6 bar is the discipline that
keeps shelved-try infrastructure useful long-term (this is the same
logic that shelved the breakout retune PR #43 and the trend retune PR
#49).

## Decision

| Overlay | Per-issue verdict | Final status |
|---|---|---|
| turn_of_month | failed aggregate Sharpe bar (PR #50) | Permanently shelved |
| fomc_drift | failed aggregate Sharpe bar (PR #50) | Permanently shelved |
| opex | failed aggregate Sharpe bar (PR #50) | Permanently shelved |
| pre_long_weekend | passed aggregate, **failed** walkforward 4-of-6 | Permanently shelved |

`config.yaml` already ships every sub-overlay `enabled: false`. No
config changes needed for this PR.

## What to do if revisiting

If the universe, regime, or strategy mix changes meaningfully (new
sleeve, regime filter overhaul, walkforward methodology change), re-run
the same A/B + walkforward harness:

```bash
# Aggregate A/B (~2 hours, six 20-min runs)
python scripts/run_calendar_overlay_ab.py

# Per-window walkforward A/B (~1 hour, two 25-min runs)
python scripts/run_pre_long_weekend_walkforward.py
```

Both scripts are idempotent and write to
`backtest_results/calendar_overlay_ab/` and
`backtest_results/calendar_overlay_walkforward/` respectively.

## Files

- Walkforward summary: `backtest_results/calendar_overlay_walkforward/summary.json`
- Driver script: `scripts/run_pre_long_weekend_walkforward.py`
- Aggregate A/B writeup: `trading_bot/docs/calendar_overlay/2026-05-03_ab_report.md`

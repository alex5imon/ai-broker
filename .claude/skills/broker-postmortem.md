---
description: Signal-quality postmortem — classify closed trades (backtest or live) as TP/FP/neutral by strategy and surface weight-adjustment suggestions
---

# Signal Postmortem

Run retrospective signal-quality analysis on closed trades. Every trade is
classified as `TRUE_POSITIVE`, `FALSE_POSITIVE[_SEVERE]`, `NEUTRAL`, or
`REGIME_MISMATCH`, attributed to its `strategy_id`. Output is a
per-strategy accuracy / false-positive-rate breakdown that informs
sub-portfolio weight adjustments.

Built on the vendored `signal-postmortem` skill at
`.claude/skills/signal-postmortem/`. The adapter at
`scripts/postmortem_from_trades.py` converts our trade format to the
skill's signal schema and bypasses the FMP price fetch (we already have
ground truth exit prices).

## When to Use

- After a multi-strategy backtest, to see which strategy is producing
  clean signals vs. false positives
- Periodically (weekly) against live closed trades to calibrate virtual
  sub-portfolio weights
- When a strategy's backtest and live behaviour diverge — postmortems
  over each source separately make the gap obvious

## Step 1: Generate Postmortem Records

### From a backtest

```bash
cd /Users/alex/Broker

# Most recent backtest (find with: ls -t backtest_results/*.json | head -1)
python3 scripts/postmortem_from_trades.py \
    --backtest backtest_results/multi_strategy_<from>_to_<to>_<ts>.json
```

### From live closed trades

```bash
python3 scripts/postmortem_from_trades.py --live
```

Records are written to `reports/postmortems/pm_*.json`, one per trade.
The console prints the outcome distribution overall and per strategy.

## Step 2: Generate the Summary Report

Run the vendored analyzer over all records in `reports/postmortems/`:

```bash
python3 .claude/skills/signal-postmortem/scripts/postmortem_analyzer.py \
    --postmortems-dir reports/postmortems/ \
    --summary \
    --group-by skill \
    --output-dir reports/
```

This writes `reports/postmortem_summary_YYYY-MM-DD.md` with:

- Overall accuracy (TP / total)
- Per-strategy table: samples, accuracy, FP rate, avg return
- Outcome distribution table

Open the markdown file and show the user the per-strategy accuracy table.

## Step 3: Generate Weight-Adjustment Feedback (Optional)

When you have ≥20 signals per strategy, generate machine-readable weight
suggestions:

```bash
python3 .claude/skills/signal-postmortem/scripts/postmortem_analyzer.py \
    --postmortems-dir reports/postmortems/ \
    --generate-weight-feedback \
    --output-dir reports/
```

The output JSON under 20 samples is flagged low-confidence; don't rush to
rebalance sub-portfolios on thin data.

## Step 4: Housekeeping

Regenerating postmortems for the same backtest is idempotent — filenames
are deterministic (sha1 of strategy + ticker + entry_time). To start
clean:

```bash
rm -rf reports/postmortems/
```

Mixing backtest and live records in the same directory is supported but
the analyzer will treat them as one pool — clear the directory between
runs if you want source-specific summaries.

## Step 5: Present to the User

Report:

1. **Per-strategy TP rate** — rank strategies by accuracy. A strategy
   below 55% is a candidate for reduced sub-portfolio weight.
2. **FP-severe count** — these are trades that went materially the wrong
   way (>2% against position). Cluster by ticker if >3 from one symbol.
3. **Sample adequacy** — call out any strategy with <20 closed trades;
   recommendations are low confidence until the count grows.

If the bot is multi-strategy and one strategy is clearly underperforming,
offer to run `broker-tune` with the intent to reduce its weight or pivot
its architecture (via `strategy-pivot-designer`).

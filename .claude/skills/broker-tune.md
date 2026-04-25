---
description: Parameter tuning - analyze recent multi-market performance and recommend config adjustments for current phase
---

# Parameter Tuning

Analyze recent trading performance across both LSE and US markets and recommend specific parameter adjustments. Always present reasoning and get user approval before making changes. Parameters are phase-aware - recommendations should be appropriate for the current growth phase.

## Step 1: Read Current Configuration

Read the current `config.yaml`:

```bash
cat config.yaml
```

Note all tunable parameters and their current values. Key parameter categories:

**Risk / Position Sizing:**
- Max position size (GBP equivalent)
- Max concurrent positions (per market and total)
- Daily loss limit (GBP)
- Max daily trades
- Per-trade risk percentage

**Strategy - Scalp Parameters (Phase 3 only - not active until account > £20,000):**
- Stop loss distance (points or percentage)
- Profit target(s) / take-profit levels
- Entry/exit signal thresholds
- Sentiment score thresholds
- Cooldown periods between trades

**Strategy - Swing Parameters:**
- Swing hold time limits
- Swing profit targets (wider than scalp)
- Swing stop loss (wider than scalp)
- Overnight hold criteria
- Swing entry filters

**Market-Specific:**
- LSE schedule (08:00-16:30 London time)
- US schedule (09:30-16:00 ET)
- LSE-specific volume/spread thresholds
- US-specific volume/spread thresholds
- Per-market position limits

**Phase-Specific:**
- Phase transition thresholds (account value triggers)
- Phase-specific trade frequency limits
- Phase-specific position sizing rules
- Consecutive profitable days requirements

**FX:**
- FX conversion settings
- USD exposure limits

## Step 2: Determine Current Phase

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
try:
    cursor = conn.execute('SELECT * FROM daily_summaries ORDER BY date DESC LIMIT 1')
    row = cursor.fetchone()
    if row: print(f'Account status: {row}')
except: print('No daily_summaries table')
conn.close()
"
```

Recommendations must be appropriate for the current phase:
- **Phase 0/1** (~£950-£1,500): Conservative, single-market focus, smaller positions, tighter risk
- **Phase 2** (~£1,500-£3,000): Dual-market, can introduce swing trades, moderate sizing
- **Phase 3** (~£3,000+): Full adaptive strategy, both markets, swing + scalp

## Step 3: Read Recent Performance Data

Query the last 5 trading days from the database, broken down by market:

```bash
cd <project-root>
python3 -c "
import sqlite3, json
conn = sqlite3.connect('trading_bot/data/trading_bot.db')

# Daily summaries by market
print('=== Daily Summary by Market ===')
cursor = conn.execute('''
    SELECT date(entry_time) as day, exchange, COUNT(*) as trades,
           SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN pnl_gbp <= 0 THEN 1 ELSE 0 END) as losses,
           SUM(pnl_gbp) as gross_pnl_gbp,
           SUM(CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END) as comm_gbp,
           AVG(CASE WHEN pnl_gbp > 0 THEN pnl_gbp END) as avg_win,
           AVG(CASE WHEN pnl_gbp <= 0 THEN pnl_gbp END) as avg_loss
    FROM trades GROUP BY day, exchange ORDER BY day DESC, exchange LIMIT 20
''')
for row in cursor: print(row)

# Per-ticker breakdown
print('\n=== Per-Ticker (Last 7 Days) ===')
cursor = conn.execute('''
    SELECT ticker, exchange, COUNT(*) as trades,
           SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
           SUM(pnl_gbp) as total_pnl_gbp
    FROM trades
    WHERE date(entry_time) >= date('now', '-7 days')
    GROUP BY ticker, exchange ORDER BY total_pnl_gbp DESC
''')
for row in cursor: print(row)

# Time-of-day analysis by market
print('\n=== By Hour and Market ===')
cursor = conn.execute('''
    SELECT exchange, strftime('%H', entry_time) as hour, COUNT(*) as trades,
           SUM(pnl_gbp) as total_pnl_gbp
    FROM trades
    WHERE date(entry_time) >= date('now', '-7 days')
    GROUP BY exchange, hour ORDER BY exchange, hour
''')
for row in cursor: print(row)

conn.close()
"
```

Also check for recent config snapshots:

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
try:
    cursor = conn.execute('SELECT date, notes FROM config_snapshots ORDER BY date DESC LIMIT 10')
    for row in cursor: print(row)
except: print('No config_snapshots table found')
conn.close()
"
```

## Step 4: Analyze and Recommend

For each parameter, evaluate whether adjustment is warranted. Apply these heuristics:

**Stop Loss (per market):**
- If average loss is significantly larger than average win, tighten the stop
- If win rate is very high but many trades are stopped out early, consider loosening slightly
- LSE and US may need different stop distances due to different volatility profiles
- Recommended risk/reward ratio: at least 1:1.5

**Profit Targets (scalp vs. swing):**
- Scalp targets should be tight and quick
- Swing targets should be wider with trailing stops
- If trades consistently run past scalp targets, consider converting to swing holds
- Analyze the distribution of winning trade sizes per market

**Position Sizing:**
- Must respect settled cash constraints
- Phase-appropriate: smaller in Phase 1, scaling up in Phase 2/3
- If daily P&L variance is too high, reduce sizes
- Account for FX exposure on USD positions

**Market Allocation:**
- If one market is consistently more profitable, consider increasing allocation
- If one market has higher commission drag, adjust trade frequency
- Consider LSE during its morning (less US competition) vs. overlap hours

**Swing Parameters (Phase 2+):**
- Only recommend swing trades if account size supports it
- Swing stops should be wider but sized so GBP risk per trade stays constant
- Overnight holds increase FX risk for USD positions

**Time Filters:**
- LSE: first 15 minutes (08:00-08:15) and last 15 minutes (16:15-16:30) may be volatile
- US: first 15 minutes (09:30-09:45) often volatile
- Overlap period (14:30-16:30 London) may have unique characteristics

## Step 5: Present Recommendations

For each recommended change, present:

```
Parameter:     strategy.us.stop_loss_pct
Current value: 0.5%
Proposed:      0.4%
Phase:         Phase 1
Market:        US
Reasoning:     Average loss (£8.50) exceeds average win (£5.20) on US trades over last 5 days.
               Tightening stop loss should reduce average loss while maintaining win rate.
Evidence:      US win rate 55%, avg win £5.20, avg loss £8.50, profit factor 0.85
Confidence:    HIGH / MEDIUM / LOW
```

## Step 6: Apply Changes (With Approval)

If the user approves specific changes, edit `config.yaml` using the Edit tool. Change only the approved parameters.

After making changes, snapshot the full config:

```bash
cd <project-root>
python3 -c "
import sqlite3, json, yaml
from datetime import datetime
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
with open('config.yaml') as f:
    config = yaml.safe_load(f)
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
conn.execute('''
    INSERT INTO config_snapshots (date, config_json, notes)
    VALUES (?, ?, ?)
''', (today, json.dumps(config), 'Parameter adjustment: [description]'))
conn.commit()
conn.close()
"
```

Replace `[description]` with a summary of what was changed and why.

## Step 7: Check for Iteration Stagnation

After every tuning round, append the resulting backtest eval to the
per-strategy iteration history and run the stagnation detector. This is
how we find out when parameter tuning has hit a local optimum and a
structural pivot is needed rather than more knob-twiddling.

```bash
cd <project-root>

# 1. Run a backtest with the new config (see broker-backtest.md)
python -m trading_bot.multi_strategy_backtest --from 2017-01-01 --to 2018-01-01 --spy

# 2. Score it per strategy (writes reports/backtest_eval_<strategy>_<ts>.json)
python3 scripts/evaluate_backtest_from_json.py --latest

# 3. Append the eval to that strategy's iteration history, noting the change
python3 .claude/skills/strategy-pivot-designer/scripts/detect_stagnation.py \
    --append-eval reports/backtest_eval_mean_reversion_<ts>.json \
    --history reports/iteration_history_mean_reversion.json \
    --strategy-id mean_reversion \
    --changes "Tightened stop_loss_pct 2.0 -> 1.5"

# 4. Detect stagnation once ≥3 iterations exist
python3 .claude/skills/strategy-pivot-designer/scripts/detect_stagnation.py \
    --history reports/iteration_history_mean_reversion.json \
    --output-dir reports/
```

The detector returns one of three recommendations:

- **`continue`**: keep iterating on parameters — progress is still being made
- **`pivot`**: parameter search exhausted; the strategy's architecture
  needs to change (different entry trigger, different exit logic, etc.).
  Triggers include `plateau`, `overfitting`, `cost_defeat`, `tail_risk`.
- **`abandon`**: multiple severe triggers; the core hypothesis is likely
  wrong

When `pivot` is returned, surface the trigger IDs to the user and suggest
a structural redesign rather than another parameter sweep. The
`references/pivot_techniques.md` under the skill has three patterns
(assumption inversion, archetype switch, objective reframe).

## Step 8: Summarize

Present a final summary of all changes made (or no changes if the user declined). Note which phase the changes are optimized for, and remind the user that parameter changes take effect on the next bot restart unless hot-reload is supported.

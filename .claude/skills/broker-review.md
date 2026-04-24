---
description: End-of-day performance review - analyze trades by market, P&L in GBP, phase progress, and compound growth tracking
---

# End-of-Day Performance Review

Perform a comprehensive review of today's trading performance across both markets. All P&L figures must be presented in GBP. Compare against compound growth targets and suggest improvements.

## Step 1: Read Today's Trades

Query the database for all of today's trades, split by market:

```bash
cd <project-root>
python3 -c "
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
conn = sqlite3.connect('trading_bot/data/trading_bot.db')

for label, where in [('LSE', \"exchange = 'LSE'\"), ('US', \"exchange IN ('NYSE', 'NASDAQ')\")]:
    print(f'\n=== {label} Trades ===')
    cursor = conn.execute(f'''
        SELECT ticker, side, entry_price, exit_price, pnl, pnl_gbp,
               commission, CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END AS commission_gbp,
               entry_time, exit_time, currency, exchange
        FROM trades WHERE date(entry_time) = ? AND {where}
        ORDER BY entry_time
    ''', (today,))
    rows = cursor.fetchall()
    if rows:
        for row in rows: print(row)
    else:
        print('  No trades')

conn.close()
"
```

Also check for the daily report file:
- `reports/daily_YYYY-MM-DD.html`

## Step 2: Summarize Key Metrics (By Market and Combined)

Calculate and present for each market separately and combined:

- **Total trades** executed
- **Win rate** (% of trades with positive P&L)
- **Gross P&L in GBP** (convert USD P&L using the day's FX rate)
- **Total commissions in GBP**
- **Net P&L in GBP**
- **Average win** and **average loss** (in GBP)
- **Profit factor** (gross wins / gross losses)
- **Largest single win** and **largest single loss** (in GBP)
- **Average hold time** per trade

Present as:

```
                   LSE          US           Combined
Trades:            X            X            X
Win rate:          XX.X%        XX.X%        XX.X%
Gross P&L:         £X.XX        £X.XX        £X.XX
Commissions:       £X.XX        £X.XX        £X.XX
Net P&L:           £X.XX        £X.XX        £X.XX
Avg win:           £X.XX        £X.XX        £X.XX
Avg loss:          £X.XX        £X.XX        £X.XX
Profit factor:     X.XX         X.XX         X.XX
Best trade:        £X.XX        £X.XX        £X.XX
Worst trade:       £X.XX        £X.XX        £X.XX
Avg hold time:     Xm           Xm           Xm
```

## Step 3: Compound Growth Tracking

Compare today's performance against the daily compound growth target (0.3-0.5% per day):

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
try:
    cursor = conn.execute('''
        SELECT date, account_value_gbp, daily_return_pct
        FROM daily_summaries
        ORDER BY date DESC LIMIT 10
    ''')
    rows = cursor.fetchall()
    for row in rows: print(row)
except Exception as e: print(f'Error: {e}')
conn.close()
"
```

Report:
- Today's return as % of account value
- Whether it meets the 0.3-0.5% daily target
- Running compound return over the last week and month
- Projected account value at current growth rate (30, 60, 90 days out)

## Step 4: FX Impact Analysis

For USD positions, show the FX impact:

- GBP/USD rate used for today's conversions
- How much FX movement helped or hurt GBP-equivalent P&L
- Net FX gain/loss on USD positions

```bash
cd <project-root>
python3 -c "
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
cursor = conn.execute('''
    SELECT COUNT(*) as usd_trades,
           SUM(net_pnl) as usd_pnl_in_usd,
           SUM(pnl_gbp) as usd_pnl_in_gbp,
           AVG(fx_rate) as avg_fx_rate
    FROM trades
    WHERE date(entry_time) = ? AND currency = 'USD'
''', (today,))
row = cursor.fetchone()
if row and row[0]:
    print(f'USD trades: {row[0]}')
    print(f'USD P&L (USD): \${row[1]:.2f}')
    print(f'USD P&L (GBP): £{row[2]:.2f}')
    print(f'Avg FX rate: {row[3]:.4f}')
else:
    print('No USD trades today')
conn.close()
"
```

This is important because a profitable USD trade can become less profitable (or unprofitable) after FX conversion if GBP strengthened during the holding period.

## Step 5: Phase Progress

Report current phase and progress:

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
try:
    cursor = conn.execute('SELECT * FROM daily_summaries ORDER BY date DESC LIMIT 1')
    row = cursor.fetchone()
    if row: print(f'Latest: {row}')
except: pass
conn.close()
"
```

- Current phase (0, 1, 2, 3)
- Account value vs. phase transition threshold
- Consecutive profitable days count
- Any phase transition criteria that are close to being met

## Step 6: Compare to Recent History

Read the last 5 trading days:

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
cursor = conn.execute('''
    SELECT date(entry_time) as day, exchange,
           COUNT(*) as trades,
           SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate,
           SUM(pnl_gbp) as gross_pnl_gbp,
           SUM(CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END) as total_comm_gbp
    FROM trades
    GROUP BY day, exchange
    ORDER BY day DESC, exchange
    LIMIT 20
''')
for row in cursor: print(row)
conn.close()
"
```

Present a comparison table:

```
Date        Market  Trades  Win Rate  Gross (GBP)  Comms (GBP)  Net (GBP)  Daily %
----------  ------  ------  --------  -----------  -----------  ---------  -------
2026-04-16  LSE     ...     ...       ...          ...          ...        ...
2026-04-16  US      ...     ...       ...          ...          ...        ...
2026-04-15  LSE     ...     ...       ...          ...          ...        ...
```

Note any improving or deteriorating trends.

## Step 7: Flag Concerns

Automatically flag these issues if detected:

- **Win rate below 50%** - strategy may need adjustment
- **High commissions** - commissions exceed 30% of gross P&L
- **Concentration risk** - more than 50% of P&L from a single ticker
- **Losing streak** - 3+ consecutive losses at any point during the day
- **Net negative day** - overall loss for the day
- **Below growth target** - net return below 0.3% for the day
- **Deteriorating trend** - 3+ consecutive days of declining net P&L
- **FX drag** - FX conversion reduced USD P&L by more than 10%
- **One market underperforming** - consistent losses in one market but not the other

For each flag, explain why it matters and what might be causing it.

## Step 8: Suggest Improvements

Based on the data, suggest specific changes:

- If one market is consistently underperforming: consider adjusting that market's parameters or reducing allocation
- If win rate is low: consider tightening entry criteria
- If average loss > average win: consider tightening stop loss
- If a specific ticker is consistently losing: recommend removing it from watchlist
- If commissions are high: consider fewer trades or focus on higher-conviction setups
- If growth target is not being met: analyze whether the issue is win rate, position sizing, or trade frequency

Present each recommendation with the specific config.yaml parameter and suggested value.

## Step 9: Ask User for Action

After presenting the review, ask the user:

1. Would you like to make any of the suggested config changes?
2. Should any tickers be added or removed from either watchlist?
3. Should we adjust the allocation between LSE and US?
4. Any other adjustments for tomorrow?

If the user approves changes, edit `config.yaml` accordingly and log the changes.

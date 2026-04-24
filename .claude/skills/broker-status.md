---
description: Check trading bot health - process status, Alpaca positions, P&L in GBP, phase progress, and errors
---

# Trading Bot Status Check

> **NOTE (2026-04-17)**: Bot is **US-only via Alpaca** now. Commands below referencing LSE markets, IB Gateway, `ib_async`, or port 4001 are **outdated**. Positions come from `TradingClient.get_all_positions()`; FX comes from `open.er-api.com`.

Run the following checks and present a clear status report to the user.

## Step 0: Check Health Endpoint

If the bot is running, query the health endpoint first (this is the authoritative real-time source):

```bash
curl -s http://localhost:8080/health 2>/dev/null | python3 -m json.tool
```

If this returns valid JSON, use it as the primary data source for gateway status, open positions, P&L, and phase info. Fall back to SQLite queries in subsequent steps only if the health endpoint is unavailable.

## Step 1: Check Bot Process

Verify the bot is running:

```bash
ps aux | grep "trading_bot.main" | grep -v grep
```

Report whether the process is alive and its PID, CPU usage, and uptime.

## Step 2: Check IB Gateway Connection

Test gateway connectivity on port 4001:

```bash
nc -z -w 3 127.0.0.1 4001
```

Report whether the gateway is reachable.

## Step 3: Check Market Status

Determine which markets are currently open:

```bash
python3 -c "
from datetime import datetime
from zoneinfo import ZoneInfo

lon = datetime.now(ZoneInfo('Europe/London'))
et = datetime.now(ZoneInfo('US/Eastern'))

lse_open = lon.weekday() < 5 and (
    (8 <= lon.hour < 16) or (lon.hour == 16 and lon.minute < 30)
)
us_open = et.weekday() < 5 and ((et.hour == 9 and et.minute >= 30) or (10 <= et.hour < 16))

print(f'London:   {lon.strftime(\"%H:%M %Z\")} - LSE {\"OPEN\" if lse_open else \"CLOSED\"}')
print(f'New York: {et.strftime(\"%H:%M %Z\")} - US {\"OPEN\" if us_open else \"CLOSED\"}')
"
```

Note: This is a simplified check and does not account for holidays. Flag if it is a potential holiday.

## Step 4: Read Today's Trading Log

Find and read today's log file. Check common locations:

- `/Users/alex/Broker/logs/trading_YYYY-MM-DD.log`
- `/Users/alex/Broker/trading_bot.log`

Use today's date. Read the last 100 lines to get recent activity.

## Step 5: Current Open Positions (Both Markets)

Query for open positions across both LSE and US:

```bash
cd /Users/alex/Broker
python3 -c "
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
cursor = conn.execute('SELECT * FROM positions WHERE status = \"open\" ORDER BY exchange, ticker')
rows = cursor.fetchall()
if rows:
    for row in rows: print(row)
else:
    print('No open positions')
conn.close()
"
```

Report each open position with: ticker, market (LSE/US), side, entry price, currency, current unrealized P&L (converted to GBP for USD positions), and entry time.

## Step 6: Today's P&L Summary (GBP)

Query the database for today's closed trades, converting USD P&L to GBP:

```bash
cd /Users/alex/Broker
python3 -c "
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
today = datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
conn = sqlite3.connect('trading_bot/data/trading_bot.db')

# LSE trades
cursor = conn.execute('''
    SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(commission), 0)
    FROM trades WHERE date(entry_time) = ? AND exchange = 'LSE'
''', (today,))
lse = cursor.fetchone()
print(f'LSE - Trades: {lse[0]}, Gross: £{lse[1]:.2f}, Comm: £{lse[2]:.2f}')

# US trades
cursor = conn.execute('''
    SELECT COUNT(*), COALESCE(SUM(pnl), 0), COALESCE(SUM(commission), 0),
           COALESCE(SUM(pnl_gbp), 0),
           COALESCE(SUM(CASE WHEN currency = 'GBP' THEN commission ELSE commission / fx_rate END), 0)
    FROM trades WHERE date(entry_time) = ? AND exchange IN ('NYSE', 'NASDAQ')
''', (today,))
us = cursor.fetchone()
print(f'US  - Trades: {us[0]}, Gross: \${us[1]:.2f}, Comm: \${us[2]:.2f} (GBP: £{us[3]:.2f}, Comm GBP: £{us[4]:.2f})')

conn.close()
"
```

## Step 7: Phase and Progress

Check the current phase and progress toward the next phase:

```bash
cd /Users/alex/Broker
python3 -c "
import sqlite3, yaml
conn = sqlite3.connect('trading_bot/data/trading_bot.db')

# Get current account value / phase info
try:
    cursor = conn.execute('SELECT * FROM daily_summaries ORDER BY date DESC LIMIT 1')
    row = cursor.fetchone()
    if row: print(f'Account status: {row}')
except: print('No daily_summaries table')

# Get trade count for today
try:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Europe/London')).strftime('%Y-%m-%d')
    cursor = conn.execute('SELECT COUNT(*) FROM trades WHERE date(entry_time) = ?', (today,))
    count = cursor.fetchone()[0]
    print(f'Trades today: {count}')
except Exception as e: print(f'Error: {e}')

conn.close()

# Check config for daily limit and phase thresholds
try:
    with open('/Users/alex/Broker/config.yaml') as f:
        config = yaml.safe_load(f)
    risk = config.get('risk', {})
    print(f'Daily trade limit: {risk.get(\"max_daily_trades\", \"not set\")}')
except: pass
"
```

Report:
- Current phase (0, 1, 2, or 3)
- Account value in GBP
- Progress toward next phase threshold
- Today's trade count vs. daily limit

## Step 8: Settled vs. Unsettled Cash

Report the breakdown of settled and unsettled cash:
- All equity trades settle T+1 (US moved to T+1 in May 2024)
- Show how much buying power is actually available

```bash
cd /Users/alex/Broker
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
cursor = conn.execute('''
    SELECT SUM(amount) as pending FROM settlements WHERE settled = 0
''')
unsettled = cursor.fetchone()[0] or 0
print(f'Unsettled cash: £{unsettled:.2f}')
conn.close()
"
```

## Step 9: Errors and Warnings

Search today's log for errors and warnings:

```bash
grep -i "ERROR\|WARNING\|CRITICAL" /Users/alex/Broker/logs/trading_$(date +%Y-%m-%d).log | tail -20
```

Categorize and summarize any issues found.

## Present Summary

Format the output as a clear status dashboard:

```
=== Trading Bot Status ===
Bot Process:     RUNNING (PID 12345) / NOT RUNNING
IB Gateway:      CONNECTED / DISCONNECTED
Current Phase:   Phase X

--- Markets ---
LSE:             OPEN / CLOSED    (London: HH:MM GMT/BST)
US:              OPEN / CLOSED    (New York: HH:MM ET)

--- Open Positions ---
Market  Ticker  Side  Entry     Unrealized P&L (GBP)
------  ------  ----  --------  --------------------
LSE     VOD     Long  £1.23     +£4.50
US      AAPL    Long  $178.50   +£12.30  ($15.60)
[or "No open positions"]

--- Today's P&L (GBP) ---
              Trades  Gross     Comms     Net
LSE:          X       £X.XX     £X.XX     £X.XX
US:           X       £X.XX     £X.XX     £X.XX
Total:        X       £X.XX     £X.XX     £X.XX

--- Cash ---
Settled:       £X.XX
Unsettled:     £X.XX
Total:         £X.XX

--- Progress ---
Trades today:  X / Y (daily limit)
Phase target:  £X,XXX (currently £X.XX - XX% progress)

--- Alerts ---
[any errors/warnings or "No issues detected"]
```

---
description: Morning startup - run pre-flight checks and start the trading bot
---

# Trading Bot Startup Sequence

> **NOTE (2026-04-17)**: The bot now trades **US equities only** via Alpaca (no LSE). Commands below referencing IB Gateway, `ib_async`, or LSE market hours are **outdated**. Replace IB Gateway health checks with `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` verification and a REST ping to `TradingClient`.

Run the following pre-flight checks step by step before starting the bot. Report the status of each check clearly. If any critical check fails, stop and report the issue - do not start the bot.

## Step 1: Check IB Gateway Connectivity

Test whether IB Gateway is reachable on port 4001:

```bash
nc -z -w 3 127.0.0.1 4001
```

If this fails, tell the user to start IB Gateway and wait for it to be ready. This is a **blocking** check - the bot cannot run without it.

## Step 2: Validate config.yaml

Verify that `config.yaml` exists in the project root (`config.yaml`) and is valid YAML:

```bash
python3 -c "import yaml; yaml.safe_load(open('config.yaml'))"
```

Check that required keys are present (at minimum: `watchlist`, `risk`, `strategy` sections, plus `lse` and `us` market sections).

## Step 3: Check caffeinate

Check if `caffeinate` is running to prevent the Mac from sleeping during trading:

```bash
pgrep caffeinate
```

If not running, start it:

```bash
caffeinate -dims &
```

## Step 4: Verify Network Connectivity

Check internet access by pinging a reliable endpoint:

```bash
curl -s --max-time 5 -o /dev/null -w "%{http_code}" https://www.google.com
```

Should return 200. This is a **blocking** check.

## Step 5: Check If Trading Day (Both Markets)

Determine if today is a trading day for LSE and/or US markets:

```bash
python3 -c "
from datetime import datetime
from zoneinfo import ZoneInfo

et_now = datetime.now(ZoneInfo('US/Eastern'))
lon_now = datetime.now(ZoneInfo('Europe/London'))

print(f'US/Eastern:      {et_now.strftime(\"%A %Y-%m-%d %H:%M:%S %Z\")}')
print(f'Europe/London:   {lon_now.strftime(\"%A %Y-%m-%d %H:%M:%S %Z\")}')
print(f'Weekend:         {et_now.weekday() >= 5}')
print()
print('Check for market holidays:')
print('  US holidays: New Year, MLK Day, Presidents Day, Good Friday, Memorial Day,')
print('               Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas')
print('  LSE holidays: New Year, Good Friday, Easter Monday, Early May Bank Holiday,')
print('                Spring Bank Holiday, Summer Bank Holiday, Christmas, Boxing Day')
"
```

Both markets could be independently open or closed. Determine the status of each:
- **LSE**: closed on UK bank holidays
- **US**: closed on US federal holidays

If both markets are closed today, inform the user and stop.

## Step 6: Phase 0 Detection

Check if this is first run or if there are existing positions that have not been assessed:

```bash
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
try:
    cursor = conn.execute('''
        SELECT COUNT(*) FROM phase_transitions WHERE from_phase = 0
    ''')
    phase0_done = cursor.fetchone()[0] > 0
    if not phase0_done:
        print('PHASE 0 REQUIRED')
    else:
        print('Phase 0 complete')
except Exception as e:
    print(f'Could not check phase transitions: {e}')
conn.close()
"
```

If unassessed positions exist, inform the user that Phase 0 cleanup will run first to evaluate existing holdings before normal trading begins.

## Step 7: Determine Startup Mode

Based on current times in both timezones, determine which markets are active:

| LSE (London Time)        | US (Eastern Time)        | Mode             | Flags                        |
|--------------------------|--------------------------|------------------|------------------------------|
| Before 08:00             | Before 09:30             | Pre-market scan  | `--mode premarket`           |
| 08:00 - 14:30            | Before 09:30             | LSE only         | `--mode lse-only`            |
| 08:00 - 16:30            | 09:30 - 16:00            | Both markets     | `--mode both`                |
| After 16:30              | 09:30 - 15:30            | US only          | `--mode us-only`             |
| After 16:30              | 15:30 - 16:00            | Close only       | `--mode close-only`          |
| After 16:30              | After 16:00              | All closed       | Do not start; inform user    |

Note: The LSE/US overlap window is approximately 14:30-16:30 London / 09:30-11:30 ET. Adjust for BST/GMT as appropriate.

If Phase 0 is required (from Step 7), append `--phase0` to the flags.

## Step 8: Start the Bot

Navigate to the project directory and start the bot:

```bash
cd <project-root>
nohup python -m trading_bot.main [flags from Step 8] >> ~/trading_bot_reports/bot_$(date +%Y-%m-%d).log 2>&1 &
echo "Bot started with PID $!"
```

This runs in the background with output redirected to a dated log file. Report the PID once started.

**IMPORTANT: This is a LIVE account. Confirm with the user before starting if there is any doubt about the mode or configuration.**

## Summary

After all checks, present a summary table:

```
Pre-flight Check           Status
-------------------------  ------
IB Gateway (4001)          OK / FAIL
config.yaml                OK / FAIL
caffeinate                 OK / Started
Network                    OK / FAIL
LSE market today           Open / Closed (holiday)
US market today            Open / Closed (holiday)
Settled cash (GBP)         £X.XX
Phase 0 needed             Yes / No
Time (London)              HH:MM GMT/BST
Time (New York)            HH:MM ET
Startup mode               [mode]
Bot started                PID [number]
```

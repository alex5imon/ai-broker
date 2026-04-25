---
description: Debug trading bot issues - analyze errors, diagnose problems, and suggest fixes
---

# Trading Bot Debugging

> **NOTE (2026-04-17)**: The project migrated from Interactive Brokers to Alpaca. Commands below referencing IB Gateway, `ib_async`, or port 4001 are **outdated**. Use Alpaca equivalents: check `ALPACA_API_KEY`/`ALPACA_SECRET_KEY` in `.env`; use `alpaca-py` not `ib_async`; the account is US-only (no LSE). The core debugging workflow (read logs, inspect DB, check state) is still valid.

Investigate and diagnose issues with the trading bot. Read logs, identify root causes, suggest fixes, and optionally restart the bot.

## Step 1: Read Today's Error Log

Check for a dedicated error log and the main trading log:

```bash
# Check for error-specific log
ls -la logs/*error* 2>/dev/null
ls -la logs/*$(date +%Y-%m-%d)* 2>/dev/null

# Read recent errors from the main log
grep -n "ERROR\|CRITICAL\|EXCEPTION\|Traceback" logs/trading_$(date +%Y-%m-%d).log 2>/dev/null | tail -30
```

If no dated log exists, check the default log location:

```bash
grep -n "ERROR\|CRITICAL\|EXCEPTION\|Traceback" trading_bot.log 2>/dev/null | tail -30
```

## Step 2: Get Context Around Errors

For each error found, read the surrounding lines to understand what was happening:

Use the Read tool with appropriate offset and limit parameters to read context around the error line, or use:

```bash
grep -n -B 10 -A 5 'ERROR\|CRITICAL' logs/trading_$(date +%Y-%m-%d).log
```

Look for patterns - are errors clustered at certain times? Do they repeat?

## Step 3: Check for Common Issues

### Gateway Disconnects
```bash
grep -i "disconnect\|connection.*lost\|connection.*reset\|reconnect\|timeout" logs/trading_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
```

**Common causes:** IB Gateway restart, network blip, API rate limiting
**Fix:** Bot should auto-reconnect. If not, check the reconnection logic in the connection manager module.

### API Failures
```bash
grep -i "api.*error\|request.*failed\|rate.*limit\|403\|429\|500\|502\|503" logs/trading_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
```

**Common causes:** Finnhub rate limits, network issues, API key expiration
**Fix:** Check API key validity, verify rate limiting backoff is working, check network connectivity.

### Order Rejections
```bash
grep -i "reject\|order.*error\|insufficient\|margin\|not.*permitted" logs/trading_$(date +%Y-%m-%d).log 2>/dev/null | tail -20
```

**Common causes:** Insufficient buying power, market closed, invalid order parameters, PDT restrictions
**Fix:** Check account balance, verify market hours, review order parameters in config.yaml.

### Unhandled Exceptions
```bash
grep -A 10 "Traceback" logs/trading_$(date +%Y-%m-%d).log 2>/dev/null | tail -40
```

**Fix:** Read the full traceback, identify the source file and line, examine the code for the bug.

### Process Crashes
```bash
# Check if bot is still running
ps aux | grep "trading_bot.main" | grep -v grep

# Check system logs for crash info
log show --predicate 'processImagePath contains "python"' --last 1h --style compact 2>/dev/null | tail -20
```

## Step 4: Check System Health

```bash
# Disk space
df -h .

# Memory usage
vm_stat | head -10

# Check if gateway is up
nc -z -w 3 127.0.0.1 4001 && echo "Gateway: UP" || echo "Gateway: DOWN"

# Check database integrity
cd <project-root>
python3 -c "
import sqlite3
conn = sqlite3.connect('trading_bot/data/trading_bot.db')
result = conn.execute('PRAGMA integrity_check').fetchone()
print(f'Database integrity: {result[0]}')
conn.close()
" 2>/dev/null || echo "Database check failed"
```

## Step 5: Diagnose and Suggest Fixes

Based on the errors found, present a diagnosis:

```
=== Diagnosis ===
Issue:       [concise description of the problem]
Root cause:  [what's causing it]
Severity:    CRITICAL / HIGH / MEDIUM / LOW
Impact:      [what effect this has on trading]
Fix:         [specific steps to resolve]
```

If the issue is in the code, read the relevant source file and suggest a patch. If the issue is configuration-related, show the specific config.yaml change needed. If the issue is environmental (network, gateway, etc.), provide the commands to fix it.

## Step 6: Optionally Restart the Bot

If the issue is resolved (or if a restart is the fix), ask the user if they want to restart:

1. Gracefully stop the current bot process:
   ```bash
   # Find and signal the bot to shut down gracefully
   pkill -SIGTERM -f "trading_bot.main"
   sleep 3
   # Verify it stopped
   ps aux | grep "trading_bot.main" | grep -v grep
   ```

2. If it did not stop, force kill:
   ```bash
   pkill -SIGKILL -f "trading_bot.main"
   ```

3. Restart using the startup skill (invoke `/broker-start`) or directly:
   ```bash
   cd <project-root>
   nohup python -m trading_bot.main > /dev/null 2>&1 &
   echo "Bot restarted with PID $!"
   ```

4. Verify the bot is running and check the first few log lines for successful startup.

**Important:** Never restart the bot during market hours without user confirmation. Open positions may be affected.

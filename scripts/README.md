# scripts/

## smoke_paper.py

~30s integration smoke test for the live Alpaca paper path. Exercises
code paths that backtests can't reach: REST auth, historical bars,
websocket subscription, bracket-order plumbing, and DB schema.

### Usage

```bash
cd /Users/alex/Broker
source .venv/bin/activate
SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())') \
    python scripts/smoke_paper.py
```

Exits 0 on success (prints `SMOKE OK`), 1 on any check failure (prints
`SMOKE FAILED: <check>`).

### Checks

1. **connect** — `TradingClient.get_account()`, asserts ACTIVE.
2. **historical_data** — 5-min SPY bars via IEX feed (free-tier); asserts
   >= 10 bars and newest within 5 days (tolerates weekends/holidays).
3. **websocket_data** — subscribes SPY trades+quotes; 30s wait. Zero
   ticks off-hours is a WARN, not a fail (IEX paper is genuinely sparse).
4. **bracket_order** — submits a BUY LIMIT far below market with
   TP+SL bracket, then cancels. Proves the bracket plumbing works.
   Any order created is cancelled in a `finally` block + a defensive
   `cancel_orders()` sweep, so repeated runs leave nothing open.
5. **position_attribution** — PRAGMA-checks that `positions` has
   `strategy_id` and `highest_price` columns.

### When to run

Before every live/paper start after touching execution, data, or DB
code. `start_bot.sh` prints a reminder but does not run it automatically
— keep it a conscious pre-flight, not an unmonitored gate.

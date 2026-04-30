# Self-improvement agent — follow-up tasks

These are real problems uncovered while building the daily-review agent
(see PR adding `trading_bot/self_improve/`). They are deliberately scoped
out of that PR because they touch live order logic and your CLAUDE.md
says "exercise extreme care with all order logic."

## How they were found

- **Date:** 2026-04-30
- **Method:** Pulled `bot-db-25135914687` from GitHub Actions, ran the
  self-improve agent against it, postmortem returned 0 closed trades.
- **Root cause investigation:** `trades` table has 54 rows but every
  row has `strategy_id`, `exit_time`, `exit_reason`, `net_pnl` = NULL.
  `positions` table has 50 closed `overnight_drift` + 2 closed
  `mean_reversion` rows with intact strategy attribution but no exit
  data columns at all. `daily_summaries` and `order_rejections` are
  empty.

## Task #2 — Live bot is not persisting exit data

### Symptom

Closed positions never appear as complete rows in `trades`. The
self-improve agent's postmortem can never see real P&L. Daily summaries
also never get written.

### Evidence (from `bot-db-25135914687`)

```sql
-- trades: 54 entries, 0 closures
SELECT COUNT(*) FROM trades;                                  -- 54
SELECT COUNT(*) FROM trades WHERE exit_time IS NOT NULL;      -- 0
SELECT COUNT(*) FROM trades WHERE strategy_id IS NOT NULL;    -- 0
SELECT COUNT(*) FROM trades WHERE net_pnl IS NOT NULL;        -- 0

-- positions: 52 closed, strategy_id present
SELECT status, COUNT(*) FROM positions GROUP BY status;
-- CLOSED                  52
-- POSITION_OPEN            1
-- STOP_AND_TARGET_ACTIVE   2

-- positions schema has NO exit_price / exit_time / exit_reason / net_pnl columns
PRAGMA table_info(positions);

-- daily_summaries: never written
SELECT COUNT(*) FROM daily_summaries;                         -- 0
```

### What the code claims to do

- `trading_bot/db/repository.py:57` — `save_trade(conn, trade)` — inserts
  a complete row including `strategy_id`, `exit_time`, `exit_price`,
  `exit_reason`, `net_pnl`.
- `trading_bot/db/repository.py:110` — `update_trade_exit(conn, trade_id, ...)`
  — UPDATE statement that fills exit data on an existing row.
- `trading_bot/execution/order_manager.py:284` — fill-detection loop
  that maps Alpaca order ids back to active positions and calls into
  the trade-update path.
- `trading_bot/execution/order_manager.py:791-805` — direct UPDATE
  statement: `... exit_reason = ? WHERE id = ?`.

So the write path exists. What's broken is which insert path is being
called on entry, and whether the same row is being updated on exit.

### Investigation starting points

1. **Find the entry insert path.** The 54 NULL-strategy rows in `trades`
   were inserted by *something*. Grep for INSERTs into trades that
   don't include strategy_id:

   ```bash
   grep -rn "INSERT INTO trades" trading_bot/
   ```

   Compare against `save_trade()` in repository.py — there may be a
   second, abbreviated insert path that's writing entry-only rows
   without ever calling `update_trade_exit`.

2. **Verify exit path is reached.** Add a log at
   `order_manager.py:803` (just before the UPDATE) and run a tick that
   closes a position. If the log never fires, fill detection isn't
   triggering for these positions; if it fires but the UPDATE matches
   0 rows, the `WHERE id = ?` is matching the wrong primary key.

3. **Check for a row-id mismatch.** The `update_trade_exit` query takes
   a `trade_id`. If the entry path inserted a row but the exit path
   looks up by Alpaca order id (or some other key), the UPDATE will
   silently match nothing. Verify the entry insert and exit update
   agree on the join key.

4. **Cross-check `positions`.** The 52 closed positions have correct
   `strategy_id`. Whatever code closes a position knows the strategy.
   If the trades-update path is downstream of the position-close path,
   it should be inheriting that attribution — figure out why it isn't.

### Schema gap to consider

`positions` has no `exit_price`, `exit_time`, `exit_reason`, or `pnl`
columns. So even if you fix the write path, the historical 52 rows in
`positions` carry no exit info — that's why I had to build the
Alpaca backfill. Going forward you have two options:

- **Treat `trades` as source of truth** — keep `positions` as
  in-flight state only, and ensure the close path always writes to
  `trades` before deleting/updating the position row.
- **Extend `positions`** — add exit columns and write closure data
  there too. Doubles the storage but means each table is self-contained.

I'd pick option (a) — `trades` is already the audit log; `positions`
should stay a working set.

### Acceptance criteria

- After a position closes, `trades` has a row with all of: strategy_id,
  exit_time, exit_price, exit_reason, net_pnl populated.
- `daily_summaries` gets a row at end of day.
- A regression test inserts a fake fill, runs the close path, and
  asserts the trades row is complete.
- Existing `bot-db-*` artifacts can be backfilled via
  `python -m trading_bot.self_improve.backfill_cli` (already shipped).

## Task #3 — Orphaned positions on disabled strategies

### Symptom

```sql
SELECT strategy_id, status, COUNT(*) FROM positions
WHERE status != 'CLOSED' GROUP BY strategy_id, status;

-- breakout            STOP_AND_TARGET_ACTIVE   1
-- trend_following     STOP_AND_TARGET_ACTIVE   1
-- unknown             POSITION_OPEN            1
```

Both `breakout` and `trend_following` were disabled in
[`config.yaml:626,654`](../config.yaml) on 2026-04-28 after the evening
walkforward. Their allocations were set to `$0` and routed to
mean_reversion + overnight_drift. But two positions on those strategies
are still open with `STOP_AND_TARGET_ACTIVE` status — meaning Alpaca is
still holding shares with active stop and target orders that the live
bot is no longer managing.

There's also a `POSITION_OPEN` row with `strategy_id='unknown'` — that's
a position the bot inherited or couldn't attribute. Same problem in
miniature.

### Why this matters

- The disabled strategies have `enabled: false` in config, so
  `create_strategies()` skips them — no code path is monitoring those
  positions on every tick. The stop/target orders Alpaca holds will
  fire eventually, but the bot won't react to fills, won't update
  state, won't write the trade row.
- If those orders get cancelled by Alpaca (e.g. inactivity), the
  position becomes naked.
- The `unknown` position is even worse — no strategy means no exit
  policy, no risk gate review, no postmortem coverage.

### Investigation starting points

1. **Identify the actual symbols and Alpaca order ids.**

   ```sql
   SELECT id, ticker, strategy_id, status, quantity, entry_price,
          alpaca_order_id, alpaca_stop_order_id, alpaca_target_order_id,
          alpaca_trail_order_id, entry_time
   FROM positions
   WHERE status != 'CLOSED';
   ```

2. **Verify the orders are still live on Alpaca.**

   ```python
   from alpaca.trading.client import TradingClient
   from alpaca.trading.requests import GetOrdersRequest
   from alpaca.trading.enums import QueryOrderStatus
   client = TradingClient(KEY, SECRET, paper=True)
   for oid in ["<stop_id>", "<target_id>"]:
       print(client.get_order_by_id(oid))
   ```

3. **Decide the policy.** Three options, in order of preference:

   - **Adopt-and-flatten.** Re-enable the strategies just long enough
     for the next tick to manage them out (let stop/target hit naturally
     OR explicitly close at next open). Then re-disable. Lowest risk;
     no manual order ops.
   - **Manual flatten via Alpaca.** Cancel stop/target orders, market
     sell. Quick but bypasses the bot's accounting.
   - **Permanent orphan handler.** Add a tick-time pass that, for any
     position whose `strategy_id` maps to a disabled or missing
     strategy, takes ownership — cancels related orders, places a
     market exit, writes the trade row. This is the right long-term
     fix; the previous two are incident response.

4. **Add a guard.** Before disabling a strategy, the config-validation
   path should refuse if there are open positions on it (or warn
   loudly). A regression test would assert this.

### Acceptance criteria

- The 2 STOP_AND_TARGET_ACTIVE and 1 POSITION_OPEN rows in the live
  paper DB are all flattened, their trade rows are complete, and
  cash is reconciled.
- `config.validate()` (or the next-best entry point) surfaces an error
  when disabling a strategy with open positions.
- An orphan-handling code path exists for the case where a strategy
  goes missing from `STRATEGY_REGISTRY` (config rename, code deletion).

## Files referenced

- [trading_bot/execution/order_manager.py](../trading_bot/execution/order_manager.py)
- [trading_bot/db/repository.py](../trading_bot/db/repository.py)
- [trading_bot/db/schema.py](../trading_bot/db/schema.py)
- [trading_bot/strategy/strategies/__init__.py](../trading_bot/strategy/strategies/__init__.py)
- [trading_bot/self_improve/alpaca_backfill.py](../trading_bot/self_improve/alpaca_backfill.py)
- [config.yaml](../config.yaml)

## Useful queries

```sql
-- All open exposure
SELECT id, ticker, strategy_id, status, quantity, entry_price, alpaca_order_id
FROM positions WHERE status != 'CLOSED';

-- Closed positions missing exit data (would-be backfill candidates)
SELECT COUNT(*) FROM positions p
WHERE p.status = 'CLOSED' AND p.strategy_id IS NOT NULL AND p.strategy_id != 'unknown'
  AND NOT EXISTS (SELECT 1 FROM trades t WHERE t.notes = 'backfill:position:' || p.id);

-- Empty trade rows (entry-only, never closed in DB)
SELECT id, ticker, entry_time FROM trades
WHERE exit_time IS NULL OR strategy_id IS NULL ORDER BY entry_time DESC LIMIT 20;
```

## Lessons learned — Alpaca order behavior outside RTH

Captured 2026-04-30 while iterating on `trading_bot/self_improve/flatten_orphans.py`
to flatten the three orphan positions identified by Phase 1 reconcile.
Each item below cost real cycles to discover — leave them here so the
next operator does not re-learn them.

### `held_for_orders` is enforced at submission time, regardless of TIF

Submitting a flatten while a stop on the same symbol is in
`PENDING_CANCEL` returns:

```
APIError: {
  "code": 40310000,
  "message": "insufficient qty available for order (requested: 1, available: 0)",
  "existing_qty": "1",
  "held_for_orders": "1",
  "related_orders": ["<the pending-cancel stop id>"]
}
```

This fires for `TimeInForce.DAY` AND for `TimeInForce.OPG`. OPG does not
bypass the qty check — Alpaca validates `held_for_orders` at the
`POST /v2/orders` endpoint, before the order ever reaches the auction
queue. Don't reach for OPG as a workaround.

### Stop-order cancellations stay in PENDING_CANCEL until the next open

`DELETE /v2/orders/{id}` against a stop placed during a previous session
returns `200` immediately, but the order's status sits at
`PENDING_CANCEL` and the held qty stays reserved until the next
opening cross. Polling with `GetOrdersRequest(status=OPEN)` will keep
returning the same stop the entire pre-market window — there is no
amount of waiting (within reason) that releases the qty before 09:30 ET.

### `close_all_positions(cancel_orders=True)` is the broker-atomic flatten

When the planned-flatten set exactly matches the live position set,
prefer this endpoint over per-ticker submit. It cancels every open
order AND closes every position in a single broker-side operation, so
it bypasses the manual cancel + qty-release dance entirely.

Caveat: even this endpoint can return `503 Service Unavailable` from
the paper API outside RTH (observed 2026-04-30 08:29 ET against
`paper-api.alpaca.markets`). Whether that's a transient outage or a
documented restriction is unclear, but in practice the cleanest path is
to run any flatten/close operation **during regular trading hours**.
Pre-market, every available approach hits one wall or another.

### Operational rule

For one-shot flatten / liquidation tools:
- Run **inside RTH** (09:30–16:00 ET).
- Use `close_all_positions(cancel_orders=True)` when planned == live.
- Per-ticker `close_position` is the fallback when planned ⊊ live.
- Never assume a `200` from `DELETE /v2/orders` means qty has released.

For the live bot's continuous order management, the existing pattern
(bracket orders + tick-time fill detection) sidesteps this entirely
because every cancel + flatten happens during RTH by definition.
```

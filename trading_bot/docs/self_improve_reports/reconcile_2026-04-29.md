# Reconciliation report — local DB vs Alpaca

_Generated 2026-04-29T23:05:22.718765+00:00 — account `PA3WZLD8NYB2` (paper)_

- DB path: `/tmp/gha_cached.db`
- Window: `2026-03-01` to `2026-04-29`
- Alpaca positions: **3** • Alpaca orders in window: **36**
- DB rows scanned: positions=**55**, trades=**54**

- Strategy enabled map: `breakout`=off, `mean_reversion`=on, `overnight_drift`=on, `sentiment_combo`=off, `trend_following`=off

## Summary

### Positions

| Classification | Count |
|---|---|
| ACTUAL_OPEN | 0 |
| MISMATCH_QTY | 0 |
| ORPHAN_DISABLED | 2 |
| ORPHAN_UNKNOWN | 1 |
| ORPHAN_NOT_HELD | 0 |
| ACTUAL_FILL | 0 |
| PHANTOM_CLOSE | 52 |
| CLOSED_NO_EXIT | 0 |

### Trades

| Classification | Count |
|---|---|
| ENTRY_ONLY_PHANTOM | 54 |
| MISSING_STRATEGY | 0 |
| MISSING_EXIT | 0 |
| COMPLETE | 0 |

## Bug-hypothesis confirmation

Counts above map directly to the data-layer bugs in [docs/self_improve_followups.md](../docs/self_improve_followups.md):

1. **trades.strategy_id NULL on entry** — 54 ENTRY_ONLY_PHANTOM rows. Confirms `_create_position_record` (order_manager.py:868) inserts trades without strategy_id.
2. **trades exit UPDATE never matches** — 0 MISSING_EXIT rows. Confirms `_close_position` (order_manager.py:802) uses positions.id as the trades WHERE clause.
3. **Phantom CLOSED on canceled entry** — 52 PHANTOM_CLOSE rows. Confirms entry-timeout / submit-error paths stamp positions CLOSED without an actual fill ever existing.
4. **Orphans on disabled strategies** — 2 ORPHAN_DISABLED + 1 ORPHAN_UNKNOWN open rows that no live tick code is managing.
5. **DB/Alpaca quantity drift** — 0 MISMATCH_QTY + 0 ORPHAN_NOT_HELD rows where the DB believes one thing and Alpaca believes another.

## Position findings

### ORPHAN_DISABLED (2)

- **id=2** `SPY` strategy=`breakout` qty=1 status=`STOP_AND_TARGET_ACTIVE` entry_time=`2026-04-27T15:40:26.335519-04:00`
  - Evidence: strategy_id='breakout' is disabled in config; alpaca_holds=yes, db_qty=1
  - Action: Adopt-and-flatten: re-enable the strategy long enough for the next tick to manage out, OR add an orphan-handler that takes ownership at tick time. Do not edit the DB until cash is reconciled.
- **id=4** `XLRE` strategy=`trend_following` qty=20 status=`STOP_AND_TARGET_ACTIVE` entry_time=`2026-04-28T09:40:40.758560-04:00`
  - Evidence: strategy_id='trend_following' is disabled in config; alpaca_holds=yes, db_qty=20
  - Action: Adopt-and-flatten: re-enable the strategy long enough for the next tick to manage out, OR add an orphan-handler that takes ownership at tick time. Do not edit the DB until cash is reconciled.

### ORPHAN_UNKNOWN (1)

- **id=1** `QQQ` strategy=`unknown` qty=-1 status=`POSITION_OPEN` entry_time=`2026-04-27T10:26:29.851781-04:00`
  - Evidence: strategy_id='unknown'; alpaca_holds=yes
  - Action: Manual review required — no strategy means no exit policy. Inspect Alpaca for matching symbol; if held, adopt under a fallback strategy and place a manual exit. If not held, mark CLOSED with exit_reason='reconciliation_mismatch'.

### PHANTOM_CLOSE (52)

- **id=3** `XLP` strategy=`mean_reversion` qty=6.0062 status=`CLOSED` entry_time=`2026-04-27T15:40:29.122150-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-27T15:40:29.122150-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=5** `SPY` strategy=`overnight_drift` qty=1.3384 status=`CLOSED` entry_time=`2026-04-28T11:45:35.838930-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:35.838930-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=6** `QQQ` strategy=`overnight_drift` qty=1.4518 status=`CLOSED` entry_time=`2026-04-28T11:45:35.917916-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:35.917916-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=7** `XLK` strategy=`overnight_drift` qty=6.0689 status=`CLOSED` entry_time=`2026-04-28T11:45:36.019290-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.019290-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=8** `XLF` strategy=`overnight_drift` qty=18.2429 status=`CLOSED` entry_time=`2026-04-28T11:45:36.095265-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.095265-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=9** `XLV` strategy=`overnight_drift` qty=6.589 status=`CLOSED` entry_time=`2026-04-28T11:45:36.177810-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.177810-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=10** `XLY` strategy=`overnight_drift` qty=8.1193 status=`CLOSED` entry_time=`2026-04-28T11:45:36.281749-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.281749-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=11** `XLP` strategy=`overnight_drift` qty=11.3834 status=`CLOSED` entry_time=`2026-04-28T11:45:36.349008-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.349008-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=12** `XLE` strategy=`overnight_drift` qty=16.3807 status=`CLOSED` entry_time=`2026-04-28T11:45:36.427872-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.427872-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=13** `XLI` strategy=`overnight_drift` qty=5.5835 status=`CLOSED` entry_time=`2026-04-28T11:45:36.513547-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.513547-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=14** `XLB` strategy=`overnight_drift` qty=18.5348 status=`CLOSED` entry_time=`2026-04-28T11:45:36.623219-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.623219-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=15** `XLU` strategy=`overnight_drift` qty=20.494 status=`CLOSED` entry_time=`2026-04-28T11:45:36.712368-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.712368-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=16** `XLRE` strategy=`overnight_drift` qty=21.7118 status=`CLOSED` entry_time=`2026-04-28T11:45:36.782613-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.782613-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=17** `XLC` strategy=`overnight_drift` qty=8.2223 status=`CLOSED` entry_time=`2026-04-28T11:45:36.858886-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:45:36.858886-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=18** `SPY` strategy=`overnight_drift` qty=1.3387 status=`CLOSED` entry_time=`2026-04-28T11:50:38.882260-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:38.882260-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=19** `QQQ` strategy=`overnight_drift` qty=1.4524 status=`CLOSED` entry_time=`2026-04-28T11:50:39.929115-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:39.929115-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=20** `XLK` strategy=`overnight_drift` qty=6.0734 status=`CLOSED` entry_time=`2026-04-28T11:50:40.461069-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:40.461069-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=21** `XLF` strategy=`overnight_drift` qty=18.2359 status=`CLOSED` entry_time=`2026-04-28T11:50:41.206896-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:41.206896-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=22** `XLV` strategy=`overnight_drift` qty=6.5872 status=`CLOSED` entry_time=`2026-04-28T11:50:41.922859-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:41.922859-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=23** `XLY` strategy=`overnight_drift` qty=8.12 status=`CLOSED` entry_time=`2026-04-28T11:50:42.865931-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:42.865931-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=24** `XLP` strategy=`overnight_drift` qty=11.3759 status=`CLOSED` entry_time=`2026-04-28T11:50:43.425341-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:43.425341-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=25** `XLE` strategy=`overnight_drift` qty=16.3892 status=`CLOSED` entry_time=`2026-04-28T11:50:44.318501-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:44.318501-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=26** `XLI` strategy=`overnight_drift` qty=5.5846 status=`CLOSED` entry_time=`2026-04-28T11:50:45.133668-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:45.133668-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=27** `XLB` strategy=`overnight_drift` qty=18.5239 status=`CLOSED` entry_time=`2026-04-28T11:50:45.934262-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:45.934262-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=28** `XLU` strategy=`overnight_drift` qty=20.483 status=`CLOSED` entry_time=`2026-04-28T11:50:46.663838-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:46.663838-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=29** `XLRE` strategy=`overnight_drift` qty=21.7044 status=`CLOSED` entry_time=`2026-04-28T11:50:48.031049-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:48.031049-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=30** `XLC` strategy=`overnight_drift` qty=8.2201 status=`CLOSED` entry_time=`2026-04-28T11:50:48.454421-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-28T11:50:48.454421-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=31** `XLY` strategy=`mean_reversion` qty=4.2435 status=`CLOSED` entry_time=`2026-04-29T10:05:37.051283-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T10:05:37.051283-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=32** `SPY` strategy=`overnight_drift` qty=1.1042 status=`CLOSED` entry_time=`2026-04-29T11:45:41.968800-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:41.968800-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=33** `QQQ` strategy=`overnight_drift` qty=1.4382 status=`CLOSED` entry_time=`2026-04-29T11:45:42.156196-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.156196-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=34** `XLK` strategy=`overnight_drift` qty=5.5115 status=`CLOSED` entry_time=`2026-04-29T11:45:42.235559-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.235559-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=35** `XLF` strategy=`overnight_drift` qty=16.8772 status=`CLOSED` entry_time=`2026-04-29T11:45:42.318292-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.318292-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=36** `XLV` strategy=`overnight_drift` qty=6.1324 status=`CLOSED` entry_time=`2026-04-29T11:45:42.408252-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.408252-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=37** `XLY` strategy=`overnight_drift` qty=6.421 status=`CLOSED` entry_time=`2026-04-29T11:45:42.523013-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.523013-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=38** `XLP` strategy=`overnight_drift` qty=9.0717 status=`CLOSED` entry_time=`2026-04-29T11:45:42.619917-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.619917-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=39** `XLE` strategy=`overnight_drift` qty=12.8172 status=`CLOSED` entry_time=`2026-04-29T11:45:42.708201-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.708201-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=40** `XLI` strategy=`overnight_drift` qty=4.4266 status=`CLOSED` entry_time=`2026-04-29T11:45:42.798016-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.798016-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=41** `XLB` strategy=`overnight_drift` qty=12.2513 status=`CLOSED` entry_time=`2026-04-29T11:45:42.883221-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.883221-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=42** `XLU` strategy=`overnight_drift` qty=13.624 status=`CLOSED` entry_time=`2026-04-29T11:45:42.964502-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:42.964502-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=43** `XLC` strategy=`overnight_drift` qty=6.5039 status=`CLOSED` entry_time=`2026-04-29T11:45:43.155984-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:45:43.155984-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=44** `SPY` strategy=`overnight_drift` qty=1.1038 status=`CLOSED` entry_time=`2026-04-29T11:50:43.115683-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:43.115683-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=45** `QQQ` strategy=`overnight_drift` qty=1.4382 status=`CLOSED` entry_time=`2026-04-29T11:50:43.444439-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:43.444439-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=46** `XLK` strategy=`overnight_drift` qty=5.5125 status=`CLOSED` entry_time=`2026-04-29T11:50:43.735415-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:43.735415-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=47** `XLF` strategy=`overnight_drift` qty=16.8707 status=`CLOSED` entry_time=`2026-04-29T11:50:44.026949-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:44.026949-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=48** `XLV` strategy=`overnight_drift` qty=6.1268 status=`CLOSED` entry_time=`2026-04-29T11:50:44.310543-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:44.310543-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=49** `XLY` strategy=`overnight_drift` qty=6.4196 status=`CLOSED` entry_time=`2026-04-29T11:50:44.608105-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:44.608105-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=50** `XLP` strategy=`overnight_drift` qty=9.0656 status=`CLOSED` entry_time=`2026-04-29T11:50:44.878798-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:44.878798-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=51** `XLE` strategy=`overnight_drift` qty=12.8172 status=`CLOSED` entry_time=`2026-04-29T11:50:45.157415-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:45.157415-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=52** `XLI` strategy=`overnight_drift` qty=4.4218 status=`CLOSED` entry_time=`2026-04-29T11:50:45.433412-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:45.433412-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=53** `XLB` strategy=`overnight_drift` qty=12.2489 status=`CLOSED` entry_time=`2026-04-29T11:50:45.739327-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:45.739327-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=54** `XLU` strategy=`overnight_drift` qty=13.6062 status=`CLOSED` entry_time=`2026-04-29T11:50:46.018815-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:46.018815-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.
- **id=55** `XLC` strategy=`overnight_drift` qty=6.4994 status=`CLOSED` entry_time=`2026-04-29T11:50:46.505430-04:00`
  - Evidence: No BUY fill found on Alpaca within +/-6h of entry_time=2026-04-29T11:50:46.505430-04:00; alpaca_order_id=None
  - Action: Confirm with Alpaca that the entry never filled. If so, the position row is correct as CLOSED but the trades row should be deleted (or marked exit_reason='entry_canceled', net_pnl=0). Live fix: don't insert a trades row until entry fill confirms.

## Trade findings

### ENTRY_ONLY_PHANTOM (54)

- **id=1** `SPY` strategy=`NULL` qty=1 entry_time=`2026-04-27T15:40:26.335519-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='breakout' status=STOP_AND_TARGET_ACTIVE
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=2** `XLP` strategy=`NULL` qty=6.0062 entry_time=`2026-04-27T15:40:29.122150-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='mean_reversion' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=3** `XLRE` strategy=`NULL` qty=20 entry_time=`2026-04-28T09:40:40.758560-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='trend_following' status=STOP_AND_TARGET_ACTIVE
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=4** `SPY` strategy=`NULL` qty=1.3384 entry_time=`2026-04-28T11:45:35.838930-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=5** `QQQ` strategy=`NULL` qty=1.4518 entry_time=`2026-04-28T11:45:35.917916-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=6** `XLK` strategy=`NULL` qty=6.0689 entry_time=`2026-04-28T11:45:36.019290-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=7** `XLF` strategy=`NULL` qty=18.2429 entry_time=`2026-04-28T11:45:36.095265-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=8** `XLV` strategy=`NULL` qty=6.589 entry_time=`2026-04-28T11:45:36.177810-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=9** `XLY` strategy=`NULL` qty=8.1193 entry_time=`2026-04-28T11:45:36.281749-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=10** `XLP` strategy=`NULL` qty=11.3834 entry_time=`2026-04-28T11:45:36.349008-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=11** `XLE` strategy=`NULL` qty=16.3807 entry_time=`2026-04-28T11:45:36.427872-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=12** `XLI` strategy=`NULL` qty=5.5835 entry_time=`2026-04-28T11:45:36.513547-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=13** `XLB` strategy=`NULL` qty=18.5348 entry_time=`2026-04-28T11:45:36.623219-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=14** `XLU` strategy=`NULL` qty=20.494 entry_time=`2026-04-28T11:45:36.712368-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=15** `XLRE` strategy=`NULL` qty=21.7118 entry_time=`2026-04-28T11:45:36.782613-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=16** `XLC` strategy=`NULL` qty=8.2223 entry_time=`2026-04-28T11:45:36.858886-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=17** `SPY` strategy=`NULL` qty=1.3387 entry_time=`2026-04-28T11:50:38.882260-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=18** `QQQ` strategy=`NULL` qty=1.4524 entry_time=`2026-04-28T11:50:39.929115-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=19** `XLK` strategy=`NULL` qty=6.0734 entry_time=`2026-04-28T11:50:40.461069-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=20** `XLF` strategy=`NULL` qty=18.2359 entry_time=`2026-04-28T11:50:41.206896-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=21** `XLV` strategy=`NULL` qty=6.5872 entry_time=`2026-04-28T11:50:41.922859-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=22** `XLY` strategy=`NULL` qty=8.12 entry_time=`2026-04-28T11:50:42.865931-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=23** `XLP` strategy=`NULL` qty=11.3759 entry_time=`2026-04-28T11:50:43.425341-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=24** `XLE` strategy=`NULL` qty=16.3892 entry_time=`2026-04-28T11:50:44.318501-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=25** `XLI` strategy=`NULL` qty=5.5846 entry_time=`2026-04-28T11:50:45.133668-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=26** `XLB` strategy=`NULL` qty=18.5239 entry_time=`2026-04-28T11:50:45.934262-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=27** `XLU` strategy=`NULL` qty=20.483 entry_time=`2026-04-28T11:50:46.663838-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=28** `XLRE` strategy=`NULL` qty=21.7044 entry_time=`2026-04-28T11:50:48.031049-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=29** `XLC` strategy=`NULL` qty=8.2201 entry_time=`2026-04-28T11:50:48.454421-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=30** `XLY` strategy=`NULL` qty=4.2435 entry_time=`2026-04-29T10:05:37.051283-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='mean_reversion' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=31** `SPY` strategy=`NULL` qty=1.1042 entry_time=`2026-04-29T11:45:41.968800-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=32** `QQQ` strategy=`NULL` qty=1.4382 entry_time=`2026-04-29T11:45:42.156196-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=33** `XLK` strategy=`NULL` qty=5.5115 entry_time=`2026-04-29T11:45:42.235559-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=34** `XLF` strategy=`NULL` qty=16.8772 entry_time=`2026-04-29T11:45:42.318292-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=35** `XLV` strategy=`NULL` qty=6.1324 entry_time=`2026-04-29T11:45:42.408252-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=36** `XLY` strategy=`NULL` qty=6.421 entry_time=`2026-04-29T11:45:42.523013-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=37** `XLP` strategy=`NULL` qty=9.0717 entry_time=`2026-04-29T11:45:42.619917-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=38** `XLE` strategy=`NULL` qty=12.8172 entry_time=`2026-04-29T11:45:42.708201-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=39** `XLI` strategy=`NULL` qty=4.4266 entry_time=`2026-04-29T11:45:42.798016-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=40** `XLB` strategy=`NULL` qty=12.2513 entry_time=`2026-04-29T11:45:42.883221-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=41** `XLU` strategy=`NULL` qty=13.624 entry_time=`2026-04-29T11:45:42.964502-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=42** `XLC` strategy=`NULL` qty=6.5039 entry_time=`2026-04-29T11:45:43.155984-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=43** `SPY` strategy=`NULL` qty=1.1038 entry_time=`2026-04-29T11:50:43.115683-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=44** `QQQ` strategy=`NULL` qty=1.4382 entry_time=`2026-04-29T11:50:43.444439-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=45** `XLK` strategy=`NULL` qty=5.5125 entry_time=`2026-04-29T11:50:43.735415-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=46** `XLF` strategy=`NULL` qty=16.8707 entry_time=`2026-04-29T11:50:44.026949-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=47** `XLV` strategy=`NULL` qty=6.1268 entry_time=`2026-04-29T11:50:44.310543-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=48** `XLY` strategy=`NULL` qty=6.4196 entry_time=`2026-04-29T11:50:44.608105-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=49** `XLP` strategy=`NULL` qty=9.0656 entry_time=`2026-04-29T11:50:44.878798-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=50** `XLE` strategy=`NULL` qty=12.8172 entry_time=`2026-04-29T11:50:45.157415-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=51** `XLI` strategy=`NULL` qty=4.4218 entry_time=`2026-04-29T11:50:45.433412-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=52** `XLB` strategy=`NULL` qty=12.2489 entry_time=`2026-04-29T11:50:45.739327-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=53** `XLU` strategy=`NULL` qty=13.6062 entry_time=`2026-04-29T11:50:46.018815-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.
- **id=54** `XLC` strategy=`NULL` qty=6.4994 entry_time=`2026-04-29T11:50:46.505430-04:00` exit_time=`NULL`
  - Evidence: strategy_id IS NULL and exit_time IS NULL; paired position strategy='overnight_drift' status=CLOSED
  - Action: Live bug: order_manager._create_position_record inserts trades without strategy_id and never updates exit (positions.id is used as trade_id, so UPDATE trades WHERE id = ? misses). Either delete and rewrite via save_trade(), or backfill via alpaca_backfill keyed off positions row.

---

_This report is read-only. No DB rows were modified and no orders were submitted. Use it to plan Phase 2 (DB migration) and Phase 3 (live order logic fixes)._

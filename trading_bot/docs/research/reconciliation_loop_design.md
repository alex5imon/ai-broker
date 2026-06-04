# Design: broker-truth reconciliation loop (end the lifecycle whack-a-mole)

**Status:** Proposal / design — no code changes. For review before any implementation.
**Date:** 2026-06-04
**Motivates:** see `memory/feedback_reconcile_loop_vs_state_machine.md` and the PR-history audit below.

---

## 1. Problem

A PR-history audit (since launch, ~2 months) found **54 commits** touching the
position/exit lifecycle and DB↔broker reconciliation — the single largest
category of work — with **~1 true regression** in that span (#157, and it's
strategy code, not lifecycle). So this is **not** sloppy regression. Every fix
addressed a *distinct, real* failure mode. They cluster into three themes that
are all the **same invariant**:

| Theme | Example fixes |
|---|---|
| State persistence across stateless ticks | #114 (exit_order_id), #144 (exit_reason), #190 (CLOSING wedge) |
| Stop lost/canceled → naked / unattributed | #64, #118 (#117), #169, #172 |
| DB-vs-broker reconcile: orphan / phantom / mismatch | #65/#67/#70, #98/#100, #146, #184 |

### Root cause
The bot maintains a **parallel, optimistic state machine in SQLite**
(`ENTRY_PENDING → POSITION_OPEN → STOP_ACTIVE → CLOSING → CLOSED`) and keeps it
consistent with Alpaca **across stateless 5-minute ticks**, reconciling
divergence **reactively — one patch per new way the two can drift**:

- lost order-submit response → #64
- canceled standalone stop → #169
- after-hours market flatten canceled, id not persisted → **#190**
- exit order id only in memory → #114
- partial fill / duplicate rows → #131/#146
- entry filled but status lagged → #76/#100

Same boat, different leaks. The principle ("broker is the source of truth") is
already written into memory **three times**
(`feedback_per_strategy_vs_broker_truth`, `phantom_stop_close_of_day`,
`standalone_stop_lifecycle`) — it's *known*, but the architecture doesn't
**enforce** it: state is transitioned locally and repaired after the fact.

## 2. Current architecture (event-triggered + reactive repair)

```
tick():
  hydrate _active_orders from DB           # local state machine
  _check_order_statuses()                  # poll Alpaca orders, transition state
  check_exits()                            # strategy exits -> CLOSING
  wind_down()                              # EOD flatten -> CLOSING
  StateRecovery._reconcile()               # bolt-on: catch some divergences
  drain_disabled_sleeves()                 # bolt-on: flush orphan sleeves
  + repair_orphans.py / reconcile/ / alpaca_backfill.py   # out-of-band repair scripts
```

The DB row's `status` is **authoritative** and advanced by local events. When an
event is lost (canceled order, dropped response, after-hours reject, crash
between two writes), the row wedges or drifts, and a *new reactive patch* is
added to catch that specific case.

## 3. Proposed architecture (level-triggered reconciliation)

Adopt the **controller / reconcile-loop pattern** (Kubernetes-style): the broker
is the single source of truth for *what is held*; the DB is a **cache + intent
journal**, not an authoritative state machine.

```
tick():
  broker = snapshot(Alpaca positions + open orders)     # ONE source of truth
  intents = load_strategy_intents(DB)                   # what we MEANT to hold + stops/targets
  for each (ticker / position):
      desired = derive_desired_state(intent, broker)     # pure function
      actual  = observe(broker)
      converge(desired, actual)                          # idempotent actions only
  journal(DB, broker, actions)                          # write-after, for reporting/audit
```

`status` is **derived**, not transitioned:

| Broker says | Intent says | Derived state / action |
|---|---|---|
| held, has protective stop | open | `PROTECTED` — nothing to do |
| held, **no** stop | open | `NEEDS_STOP` — attach stop (idempotent) |
| held | exit requested (CLOSING intent) | re-submit flatten **iff RTH** + no live exit order |
| **not held** | open / CLOSING | `CLOSED` — journal exit from the fill that closed it |
| order filled | entry pending | `OPEN` — adopt broker qty/price |
| order canceled/expired | pending exit | clear intent, re-derive next tick |

Key properties:
- **Idempotent convergence**: every action ("attach stop", "flatten", "mark
  closed") is safe to repeat — a missed/duplicated tick self-corrects.
- **No wedge states**: there is no `CLOSING` that can get stuck, because
  "should be flat but broker still holds it" is re-derived and re-driven every
  tick until the broker agrees.
- **Subsumes the bug class**: #190 (CLOSING wedge), #118/#169 (naked/canceled
  stop), #65–#70/#184 (orphan/phantom/mismatch), #114/#144 (exit-id/reason
  persistence) all become **non-issues** — there's nothing to persist-across-
  ticks because state is recomputed from truth each tick.

## 4. The missing regression guard (do this FIRST — cheap, high value)

Independent of the refactor, add a **per-tick invariant check** that fails loud
(ntfy + log) when any position is inconsistent with broker truth for **> 1 tick**:

- held at broker **and** no protective stop on the book → naked (caught #118/#169)
- DB status `CLOSING`/intent-flat **and** still held → wedge (caught #190)
- DB status open **and** not held at broker → orphan (caught #65–#70)
- broker holds a ticker the DB has **no** open row for → unknown position

One assertion catches the entire class **as a class**, and gives an early-warning
signal during the migration. This is a ~1-day change and would have surfaced
XLI #147 within 5 minutes instead of 2 days.

## 5. Migration plan (incremental — NOT a big-bang rewrite)

The order path is the highest-risk code in the repo ("exercise extreme care").
Stage it so each step is independently shippable and reversible:

- **Phase 0 — Guard (1 PR):** add the §4 invariant check as an *observer* (alert
  only, takes no action). Run it for ~1 week to quantify how often each
  divergence actually fires in paper. Evidence for the rest.
- **Phase 1 — Snapshot + derive (read-only):** build `broker_snapshot()` and a
  pure `derive_desired_state(intent, broker)`. Run it each tick in **shadow
  mode** — log where derived state disagrees with the live state machine. No
  behavior change. Validates the reconciler against reality before it drives
  anything.
- **Phase 2 — Reconciler owns STATUS:** let the derived state become
  authoritative for the DB `status` column (replacing the local transitions in
  `_check_order_statuses`). Keep the existing action paths (`_place_standalone_stop`,
  `place_exit`) but trigger them from the reconciler's convergence step. Delete
  the now-dead reactive patches as their cases are provably covered.
- **Phase 3 — Retire the repair scripts:** once the in-loop reconciler is
  authoritative, `repair_orphans.py` / `reconcile/` / the `reconciliation_mismatch`
  resolve become redundant; keep only a thin nightly audit.

Each phase gates on: shadow-mode agreement ≥ N days, the §4 guard staying quiet,
and the full critical test suite green.

## 6. What this explicitly subsumes (and lets us NOT build)

- The deferred wind-down hardenings **A** (don't flatten after close) and **B**
  (persist exit id in `emergency_flatten`) become **free**: the reconciler only
  flattens during RTH and re-derives from truth, so a canceled after-hours order
  is simply re-driven next RTH tick — nothing to persist, nothing to time-gate
  specially. **Don't ship A/B as standalone patches** — they're more of the same
  whack-a-mole.
- `#190`'s watchdog stays (it's the right behavior and a safe stepping stone),
  but in the end-state it's just one rule in `derive_desired_state`.

## 7. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Reconciler bug flattens/double-trades on bad broker read | Idempotent actions; never act on a transient/None broker read (already the #190 guard pattern); shadow mode (Phase 1) before it drives anything |
| Alpaca API rate / latency from per-tick full snapshot | One `get_all_positions` + one `get_orders(open)` per tick — already roughly what the scattered checks cost combined |
| Big-bang risk on the most sensitive code | Phased, shadow-first, reversible; the guard (Phase 0) runs the whole time |
| Partial fills / fractional qty | Reconciler reads broker `qty` as truth (the #108 lesson, but now central) |

## 8. Recommendation

1. **Ship Phase 0 (the invariant guard) next** — cheap, immediately useful,
   evidence-gathering, and it would have caught the last several incidents in
   minutes.
2. **Stop shipping standalone lifecycle point-fixes** (including A/B). Fold them
   into the reconciler design.
3. Treat the reconciler as a **pre-live blocker-class** improvement: it's the
   difference between "we patch a new divergence every week" and "divergence is
   structurally impossible to persist."

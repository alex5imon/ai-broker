# scripts/

## smoke_paper.py

~30s integration smoke test for the live Alpaca paper path. Exercises
code paths that backtests can't reach: REST auth, historical bars,
websocket subscription, bracket-order plumbing, and DB schema.

### Usage

```bash
cd <project-root>
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

---

## Writing a one-shot repair script — template + checklist

Distilled from PR #110 (the pre-#108 int(qty) truncation repair). The
script + workflow + tests ran live, restored $46.99 of masked pnl across
19 rows, and were deleted in PR #111. The patterns below are what would
have made it cleaner the first time. Reach for this when the bug is in
data, not code — when a forward-only fix has already merged and you
need to heal already-affected rows.

### Decision: script vs. CLI vs. SQL one-liner

| Choice | When | Cost |
|---|---|---|
| Raw `sqlite3` UPDATE in a workflow | Tiny, no joins, no rounding edge cases, < 5 affected rows known by id | Lowest. Skip if pnl math is involved. |
| Python script + workflow_dispatch | Joins needed, math to verify, > 5 rows, want dry-run + audit log | The default. Use the template below. |
| Add a `--repair` flag to an existing CLI (e.g. `backfill_cli`) | The repair is structurally part of a recurring job and may need to run again | Most invasive. Only if you'd genuinely re-run it. |

### Anatomy of a good one-shot

1. **Default to dry-run, require `--apply` to write.**
   No `confirm: y/n` prompts — the GHA dispatch UI already serves as
   the human gate.
2. **Idempotent.** A second run with no affected rows must log
   "nothing to do" and exit 0 cleanly. Re-runs are how you verify the
   repair actually landed.
3. **Audit-logged.** Every row touched: id, ticker, before/after pnl,
   delta. The log is the audit trail GHA preserves for ~90 days.
4. **Bounded scope.** A tight WHERE clause (e.g.
   `ABS(recorded - expected) > $0.01` epsilon) so re-runs are no-ops
   and you never touch rows that aren't broken.
5. **Test the find/repair/idempotency triple.** Even for throwaway
   code, the coverage gate (currently 71%) won't let you skip tests.
   Three tests is the minimum: positive find, negative skip-correct,
   idempotent re-apply.

### Template — script skeleton

```python
# scripts/repair_<topic>_<YYYY_MM_DD>.py
"""One-shot: <one-sentence description of what data is wrong and why>.

Context — <related PR that fixed it forward-only>:
  <2–3 lines of why the fix didn't heal existing rows>

Scope:
  - Only rows where <precise filter>.
  - Epsilon: <unit>.
  - Idempotent.
"""
import argparse, logging, sqlite3, sys
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)


class AffectedRow(NamedTuple):
    """Named row schema — avoids `_, _, _, _, _, real_qty, _, _, _, real_pnl`
    positional unpacking in main(). Add a field, fix the constructor,
    done."""
    trade_id: int
    ticker: str
    real_value: float  # whatever the corrected number is
    # ... add the audit-log fields you actually need


def find_affected_rows(conn: sqlite3.Connection,
                       epsilon: float) -> list[AffectedRow]:
    cur = conn.execute(
        """
        SELECT t.id, t.ticker, p.quantity
          FROM trades t
          JOIN positions p ON ('backfill:position:' || p.id) = t.notes
         WHERE ABS(t.pnl_usd - (t.exit_price - t.entry_price) * p.quantity)
               > ?
         ORDER BY t.id
        """,
        (epsilon,),
    )
    return [AffectedRow(*row) for row in cur.fetchall()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="trading_bot/data/trading_bot.db")
    parser.add_argument("--apply", action="store_true")
    # Configurable epsilon — visible in the GHA log alongside the run.
    parser.add_argument("--epsilon", type=float, default=0.01,
                        help="Tolerance for 'already correct' (USD).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    db_path = Path(args.db)
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2

    conn = sqlite3.connect(db_path)
    try:
        rows = find_affected_rows(conn, args.epsilon)
        if not rows:
            logger.info("No rows to repair.")
            return 0
        for r in rows:
            logger.info("  id=%-4d %-5s ...", r.trade_id, r.ticker)
        if not args.apply:
            logger.info("Dry-run — pass --apply to commit.")
            return 0
        # ... UPDATE per row, then commit ...
        conn.commit()
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
```

### Template — workflow

```yaml
# .github/workflows/repair-<topic>-<YYYY-MM-DD>.yml
name: repair-<topic>
on:
  workflow_dispatch:
    inputs:
      dry_run:
        description: "Dry run (log changes without writing)"
        type: boolean
        default: true
      epsilon:
        description: "USD tolerance for 'already correct'"
        type: string
        default: "0.01"

# Own concurrency group so bot.yml's cancel-in-progress can't kill us
# mid-write. The bot's `bot-run` group and ours don't collide.
concurrency:
  group: repair-run
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  repair:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip
          cache-dependency-path: requirements.txt
      - run: pip install -r requirements.txt

      # Restore the live bot DB the same way daily-review does.
      # `bot-db-${{ github.run_id }}` becomes the new "latest" under
      # the bot-db- prefix, so the next bot.yml tick picks us up.
      - uses: actions/cache@v5
        with:
          path: trading_bot/data/trading_bot.db*
          key: bot-db-${{ github.run_id }}
          restore-keys: |
            bot-db-

      - name: Run repair (dry_run=${{ inputs.dry_run }})
        run: |
          ARGS="--epsilon ${{ inputs.epsilon }}"
          if [ "${{ inputs.dry_run }}" = "false" ]; then
            ARGS="$ARGS --apply"
          fi
          python scripts/repair_<topic>_<YYYY_MM_DD>.py $ARGS

      # Only recompute aggregates when we actually wrote. On dry-run
      # the recompute would produce output that looks like an audit
      # but proves nothing.
      - name: Recompute daily_summaries
        if: inputs.dry_run == false
        run: python -m trading_bot.self_improve.recompute_daily_summaries --days 30

      - if: always()
        uses: actions/upload-artifact@v6
        with:
          name: repair-db-${{ github.run_id }}
          path: trading_bot/data/trading_bot.db*
          retention-days: 30
```

### Template — test

```python
# trading_bot/tests/test_repair_<topic>.py
import importlib.util, sys
from pathlib import Path
import pytest

_SCRIPT = (Path(__file__).resolve().parents[2]
           / "scripts" / "repair_<topic>_<YYYY_MM_DD>.py")
# Skip cleanly if the script was already removed (post-cleanup PR).
# A bare module-level load raises AttributeError on the next import,
# which is confusing in CI logs.
if not _SCRIPT.exists():
    pytest.skip("one-shot script removed", allow_module_level=True)

_SPEC = importlib.util.spec_from_file_location("repair_topic", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["repair_topic"] = _MODULE
_SPEC.loader.exec_module(_MODULE)

# Three tests minimum:
# - find_affected_rows flags a truncated row
# - find_affected_rows skips a correct row
# - main(--apply) writes, then a second main(--apply) reports nothing to do
```

### After the live run — cleanup PR

1. Verify the GHA artifact DB matches expectations.
2. Confirm a follow-up dry-run reports "no rows to repair" (use the
   workflow's `dry_run=true` input).
3. Open a follow-up PR deleting `scripts/repair_*.py`,
   `.github/workflows/repair-*.yml`, and the test file. PR body
   should link the dispatched run URL for posterity.
4. If a similar truncation class ever recurs, restore from history
   rather than leaving dead code in the tree.

### Reference

- PR #108 — forward-fix for `int(qty)` truncation in `alpaca_backfill.py`.
- PR #110 — one-shot repair (this template's worked example).
- PR #111 — cleanup PR that deleted the one-shot.

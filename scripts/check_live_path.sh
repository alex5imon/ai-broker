#!/usr/bin/env bash
# Static checks for the live trading path.
#
# These rules encode lessons from prior incidents (PR #52, #56, the
# overnight_drift naive-date fix). They are cheap regex guards that
# catch the exact bug shape on the next pass — much cheaper than
# another manual review.
#
# Rules enforced:
#   1. Inside trading_bot/execution/, no bare client.submit_order(...).
#      All Alpaca order submissions in async code must go through
#      asyncio.to_thread to keep from blocking the tick's event loop.
#   2. Inside live-path modules, no naive date.today() / datetime.now()
#      without an explicit tz= keyword. Use trading_today() / trading_now()
#      from trading_bot.utils.time, or pass tz=TZ_EASTERN.
#   3. Inside live-path modules, no equality comparison on money-typed
#      variables (price/pnl/cash/qty/equity/...) against literal numbers.
#      Float arithmetic accumulates ±1e-15 drift; `pnl == 0` mis-classifies
#      a scratch trade as a tiny win/loss. Use `abs(x) < epsilon` or
#      `math.isclose()`.
#
# Usage:
#   bash scripts/check_live_path.sh
#
# Exits non-zero on any violation. Intended to run in CI and locally
# before pushing.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

FAIL=0

# Live-path directories to scan. Tests, backtester, and self_improve
# are intentionally excluded — they don't run on the live tick.
LIVE_PATHS=(
    "trading_bot/execution"
    "trading_bot/strategy"
    "trading_bot/gateway"
    "trading_bot/data/market_data.py"
    "trading_bot/main.py"
    "trading_bot/config.py"
    "trading_bot/health"
    "trading_bot/notifications"
)

# ---------------------------------------------------------------------
# Rule 1 — no sync submit_order in async execution code
# ---------------------------------------------------------------------
echo "[live-path] Rule 1 — no sync client.submit_order in trading_bot/execution/"
SYNC_HITS=$(
    grep -nE "client\.submit_order\(" trading_bot/execution \
        --include="*.py" -r 2>/dev/null \
        | grep -v "asyncio.to_thread" \
        | grep -v "^[^:]*:[[:space:]]*#" \
        || true
)
# The grep above prints lines that contain submit_order but not
# to_thread on the SAME line. The wrapped form is:
#   await asyncio.to_thread(
#       client.submit_order, order_data=request,
#   )
# so the submit_order line itself does not contain "asyncio.to_thread".
# We need a different check — confirm the submit_order line is part of
# a multi-line asyncio.to_thread call.
SYNC_VIOLATIONS=""
while IFS= read -r line; do
    [ -z "$line" ] && continue
    file="${line%%:*}"
    rest="${line#*:}"
    lineno="${rest%%:*}"
    # Look at the previous 2 lines for asyncio.to_thread.
    prev_block=$(sed -n "$((lineno-2)),$((lineno))p" "$file" 2>/dev/null || true)
    if ! echo "$prev_block" | grep -q "asyncio.to_thread"; then
        SYNC_VIOLATIONS="${SYNC_VIOLATIONS}${line}"$'\n'
    fi
done <<< "$SYNC_HITS"

if [ -n "$SYNC_VIOLATIONS" ]; then
    echo "  FAIL — sync client.submit_order() found in async execution code:"
    echo "$SYNC_VIOLATIONS" | sed 's/^/    /'
    echo "  Wrap with: order = await asyncio.to_thread(client.submit_order, order_data=request)"
    FAIL=1
else
    echo "  OK"
fi

# ---------------------------------------------------------------------
# Rule 2 — no naive datetime in the live path
# ---------------------------------------------------------------------
echo "[live-path] Rule 2 — no naive date.today() / datetime.now() in live code"
NAIVE_HITS=""
for path in "${LIVE_PATHS[@]}"; do
    [ -e "$path" ] || continue
    hits=$(
        grep -nE "(\bdate\.today\(\)|\bdatetime\.now\(\s*\))" "$path" \
            --include="*.py" -r 2>/dev/null \
            | grep -vE "^[^:]+:[0-9]+:[[:space:]]*#" \
            | grep -v "tests/" \
            || true
    )
    if [ -n "$hits" ]; then
        NAIVE_HITS="${NAIVE_HITS}${hits}"$'\n'
    fi
done

if [ -n "$NAIVE_HITS" ]; then
    echo "  FAIL — naive date/datetime calls in live path:"
    echo "$NAIVE_HITS" | sed 's/^/    /'
    echo "  Use trading_today() / trading_now() from trading_bot.utils.time,"
    echo "  or pass tz=TZ_EASTERN explicitly."
    FAIL=1
else
    echo "  OK"
fi

# ---------------------------------------------------------------------
# Rule 3 — no equality comparisons on money-typed variables
# ---------------------------------------------------------------------
# Catches `pnl == 0`, `price != 100.0`, etc. Float arithmetic drifts;
# a scratch trade with P&L of 1e-15 must be classified the same as 0.0.
# Allow `<= 0`, `>= 0`, `> 0`, `< 0` — those are direction checks, not
# exact-value checks. Allow `is None` / `is not None` (no equality).
echo "[live-path] Rule 3 — no equality on money-typed variables"
MONEY_VARS='price|pnl|cash|qty|quantity|equity|amount|balance|cost|fees|commission|exit_price|entry_price|stop_price|target_price|net_pnl'
MONEY_HITS=""
for path in "${LIVE_PATHS[@]}"; do
    [ -e "$path" ] || continue
    hits=$(
        grep -nE "\\b(${MONEY_VARS})[a-z_]*\\s*(==|!=)\\s*[-+]?[0-9]" "$path" \
            --include="*.py" -r 2>/dev/null \
            | grep -vE "^[^:]+:[0-9]+:[[:space:]]*#" \
            | grep -vE '"""' \
            | grep -vE "'''" \
            | grep -v "tests/" \
            || true
    )
    if [ -n "$hits" ]; then
        MONEY_HITS="${MONEY_HITS}${hits}"$'\n'
    fi
done

if [ -n "$MONEY_HITS" ]; then
    echo "  FAIL — equality comparison on money-typed variable in live path:"
    echo "$MONEY_HITS" | sed 's/^/    /'
    echo "  Use abs(x) < epsilon or math.isclose() — float drift makes"
    echo "  exact equality unreliable for money/price/qty values."
    FAIL=1
else
    echo "  OK"
fi

# ---------------------------------------------------------------------
exit "$FAIL"

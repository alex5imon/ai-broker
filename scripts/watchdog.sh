#!/usr/bin/env bash
# watchdog.sh — Detect frozen trading bot and restart it.
#
# Runs every 5 minutes via LaunchAgent. If today's bot log hasn't been
# touched in $STALE_THRESHOLD_SEC seconds during the active window
# (weekday, 07:50-21:30 London), kill the process and kickstart launchd.
#
# A tombstone at $RESTART_TOMBSTONE prevents restart loops.

set -euo pipefail

# Derive project root from this script's location (scripts/ is one level deep).
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG_DIR="${PROJECT_DIR}/logs"
WATCHDOG_LOG="${LOG_DIR}/watchdog.log"
RESTART_TOMBSTONE="${LOG_DIR}/.watchdog_last_restart"
# Find the most recently modified bot log. start_bot.sh pins its log filename
# at launch time, so a bot launched yesterday keeps writing to yesterday's
# file. Keying off today's date would falsely trip after midnight.
BOT_LOG="$(ls -t "${LOG_DIR}"/bot_*.log 2>/dev/null | head -1)"

LAUNCHD_LABEL="com.tradingbot.broker"
PROCESS_PATTERN="trading_bot.main"

STALE_THRESHOLD_SEC=600        # 10 minutes of no log activity = frozen
RESTART_COOLDOWN_SEC=600       # don't restart again within 10 minutes

# Read ntfy topic from env (set in .env or the LaunchAgent plist). Empty
# = watchdog runs silently with no push notifications.
NTFY_TOPIC="${NTFY_TOPIC:-}"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$WATCHDOG_LOG"
}

notify() {
    local title="$1"
    local body="$2"
    local priority="${3:-4}"
    if [ -z "$NTFY_TOPIC" ]; then
        return 0
    fi
    curl -fsS --max-time 5 \
        -H "Title: ${title}" \
        -H "Priority: ${priority}" \
        -H "Tags: warning,robot" \
        -d "${body}" \
        "https://ntfy.sh/${NTFY_TOPIC}" > /dev/null 2>&1 || true
}

# ---------------------------------------------------------------------------
# Active-window check: weekday, 07:50-21:30 London local time.
# Outside this window we do nothing — bot is expected to be off.
# ---------------------------------------------------------------------------
DOW=$(date +%u)            # 1=Mon ... 7=Sun
HHMM=$(date +%H%M)

if [ "$DOW" -ge 6 ]; then
    exit 0
fi
if [ "$HHMM" -lt "0750" ] || [ "$HHMM" -gt "2130" ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Cooldown: if we already restarted in the last $RESTART_COOLDOWN_SEC, skip.
# ---------------------------------------------------------------------------
if [ -f "$RESTART_TOMBSTONE" ]; then
    LAST_RESTART=$(stat -f %m "$RESTART_TOMBSTONE" 2>/dev/null || echo 0)
    NOW=$(date +%s)
    AGE=$(( NOW - LAST_RESTART ))
    if [ "$AGE" -lt "$RESTART_COOLDOWN_SEC" ]; then
        log "In cooldown (${AGE}s since last restart). Skipping."
        exit 0
    fi
fi

# ---------------------------------------------------------------------------
# Health check: is the bot process alive AND is today's log fresh?
# ---------------------------------------------------------------------------
BOT_PID=$(pgrep -f "$PROCESS_PATTERN" || true)

STALE=0
REASON=""

if [ -z "$BOT_PID" ]; then
    STALE=1
    REASON="no ${PROCESS_PATTERN} process running"
elif [ -z "$BOT_LOG" ] || [ ! -f "$BOT_LOG" ]; then
    STALE=1
    REASON="process ${BOT_PID} running but no bot_*.log in ${LOG_DIR}"
else
    LOG_MTIME=$(stat -f %m "$BOT_LOG")
    NOW=$(date +%s)
    AGE=$(( NOW - LOG_MTIME ))
    if [ "$AGE" -gt "$STALE_THRESHOLD_SEC" ]; then
        STALE=1
        REASON="log ${BOT_LOG##*/} unchanged for ${AGE}s (process ${BOT_PID})"
    fi
fi

if [ "$STALE" -eq 0 ]; then
    exit 0
fi

# ---------------------------------------------------------------------------
# Restart: kill any stuck process, drop tombstone, kickstart launchd.
# ---------------------------------------------------------------------------
log "STALE DETECTED: ${REASON}"
log "Killing process(es): ${BOT_PID:-none}"

if [ -n "$BOT_PID" ]; then
    # shellcheck disable=SC2086
    kill -TERM $BOT_PID 2>/dev/null || true
    sleep 3
    # shellcheck disable=SC2086
    kill -KILL $BOT_PID 2>/dev/null || true
fi

# Also catch the start_bot.sh wrapper if it's lingering
pkill -f "start_bot.sh" 2>/dev/null || true
sleep 1

touch "$RESTART_TOMBSTONE"

log "Kickstarting ${LAUNCHD_LABEL}..."
if launchctl kickstart -k "gui/$(id -u)/${LAUNCHD_LABEL}" 2>>"$WATCHDOG_LOG"; then
    log "Kickstart issued."
    notify "Bot Watchdog: Restarted" "Bot was frozen (${REASON}). Relaunched via launchd." 5
else
    log "ERROR: kickstart failed."
    notify "Bot Watchdog: Restart FAILED" "Frozen bot detected (${REASON}) but kickstart failed. Manual intervention needed." 5
fi

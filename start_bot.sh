#!/usr/bin/env bash
# start_bot.sh — Pre-flight checks then launch the trading bot.
#
# Called by the macOS LaunchAgent (Mon-Fri 07:45) or manually:
#   bash start_bot.sh [--dry-run]
#
# Pre-flight sequence:
#   1. Activate virtualenv
#   2. Check it's a trading day (skip weekends & holidays)
#   3. Verify Alpaca API keys are set and the REST API is reachable
#   4. Send ntfy notification
#   5. Launch the bot
#
# The bot handles its own wind-down at market close and exits cleanly.

set -euo pipefail

# ---------------------------------------------------------------------------
# Dry-run passthrough
# ---------------------------------------------------------------------------
EXTRA_ARGS=""
if [ "${1:-}" = "--dry-run" ]; then
    EXTRA_ARGS="--dry-run"
    shift
fi

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
LOG_DIR="${PROJECT_DIR}/logs"
CONFIG="${PROJECT_DIR}/config.yaml"
TODAY_LOG="${LOG_DIR}/bot_$(date +%Y-%m-%d).log"

mkdir -p "$LOG_DIR"

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$TODAY_LOG"
}

# ---------------------------------------------------------------------------
# 1. Activate virtualenv
# ---------------------------------------------------------------------------
if [ ! -f "${VENV_DIR}/bin/activate" ]; then
    log "ERROR: Virtual environment not found. Run: bash install.sh"
    exit 1
fi

source "${VENV_DIR}/bin/activate"

# Python.org's OpenSSL doesn't use the macOS keychain — point it at certifi's
# CA bundle so aiohttp/requests can verify TLS (ntfy.sh, Alpaca, Finnhub).
if [ -z "${SSL_CERT_FILE:-}" ]; then
    export SSL_CERT_FILE="$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null || true)"
    export REQUESTS_CA_BUNDLE="${SSL_CERT_FILE}"
fi

# Load environment variables (.env file with FINNHUB_API_KEY etc.)
ENV_FILE="${PROJECT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

log "=== Trading Bot Pre-Flight ==="
log "Python: $(python3 --version)"
log "Project: ${PROJECT_DIR}"

# ---------------------------------------------------------------------------
# 2. Check if today is a trading day
# ---------------------------------------------------------------------------
IS_TRADING_DAY=$(python3 -c "
from datetime import date
from trading_bot.config import Config
from trading_bot.constants import Market
c = Config.load('${CONFIG}')
print('yes' if c.is_trading_day(date.today(), Market.US) else 'no')
" 2>>"$TODAY_LOG")

if [ "$IS_TRADING_DAY" = "no" ]; then
    log "Not a trading day (weekend or holiday). Exiting."
    exit 0
fi

log "Trading day confirmed."

# ---------------------------------------------------------------------------
# 3. Check config.yaml exists and is valid
# ---------------------------------------------------------------------------
if [ ! -f "$CONFIG" ]; then
    log "ERROR: config.yaml not found at ${CONFIG}"
    exit 1
fi

python3 -c "
from trading_bot.config import Config
from trading_bot.constants import Market
c = Config.load('${CONFIG}')
print(f'  Phase: {c.get_phase().name}')
print(f'  Watchlist: {len(c.get_watchlist(Market.US))} US tickers')
" 2>>"$TODAY_LOG" | while read -r line; do log "$line"; done

# ---------------------------------------------------------------------------
# 4. Verify Alpaca credentials + REST reachability
# ---------------------------------------------------------------------------
if [ -z "${ALPACA_API_KEY:-}" ] || [ -z "${ALPACA_SECRET_KEY:-}" ]; then
    log "ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY are not set (.env or shell)."
    exit 1
fi

log "Pinging Alpaca REST API..."
if ! python3 -c "
import sys
from alpaca.trading.client import TradingClient
import yaml, os
with open('${CONFIG}') as f:
    paper = bool(yaml.safe_load(f).get('alpaca', {}).get('paper', True))
client = TradingClient(os.environ['ALPACA_API_KEY'], os.environ['ALPACA_SECRET_KEY'], paper=paper)
acct = client.get_account()
print(f'  Account: {acct.account_number} status={acct.status} equity=\${acct.equity} paper={paper}')
" 2>>"$TODAY_LOG" | while read -r line; do log "$line"; done; then
    log "ERROR: Alpaca REST ping failed — see log for traceback."
    python3 -c "
import requests
try:
    requests.post('https://ntfy.sh/trading-bot-kill-alpaca',
        data='Alpaca REST unreachable — bot did not start',
        headers={'Title': 'Bot Start FAILED', 'Priority': '5', 'Tags': 'warning'}, timeout=5)
except Exception: pass
" 2>/dev/null || true
    exit 1
fi

log "TIP: run 'python scripts/smoke_paper.py' before live-path changes to catch integration bugs the backtest can't."

# ---------------------------------------------------------------------------
# 5. Send startup notification
# ---------------------------------------------------------------------------
python3 -c "
import requests
try:
    requests.post(
        'https://ntfy.sh/trading-bot-alpaca',
        data='Pre-flight passed. Bot starting.',
        headers={'Title': 'Bot Starting', 'Priority': '3', 'Tags': 'rocket'},
        timeout=5
    )
except Exception:
    pass
" 2>/dev/null || true

log "Startup notification sent."

# ---------------------------------------------------------------------------
# 6. Launch the bot
# ---------------------------------------------------------------------------
log "Launching trading bot..."
log "Log: ${TODAY_LOG}"
log "---"

# Run the bot, tee output to both the daily log and stdout
# shellcheck disable=SC2086
python3 -m trading_bot.main --config "$CONFIG" $EXTRA_ARGS 2>&1 | tee -a "$TODAY_LOG"

EXIT_CODE=${PIPESTATUS[0]}

log "---"
log "Bot exited with code ${EXIT_CODE}"

# Send shutdown notification
python3 -c "
import requests
try:
    requests.post(
        'https://ntfy.sh/trading-bot-alpaca',
        data='Bot process exited (code ${EXIT_CODE})',
        headers={'Title': 'Bot Stopped', 'Priority': '3', 'Tags': 'checkered_flag'},
        timeout=5
    )
except Exception:
    pass
" 2>/dev/null || true

exit $EXIT_CODE

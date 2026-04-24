#!/usr/bin/env bash
# install.sh — One-time setup for the Alpaca trading bot on macOS.
#
# What it does:
#   1. Checks Python >= 3.10
#   2. Creates a virtualenv (if not already present)
#   3. Installs pip dependencies
#   4. Initialises the SQLite database
#   5. Installs a macOS LaunchAgent for daily auto-start
#   6. Verifies config.yaml exists
#
# Usage:
#   cd <project-root>
#   bash install.sh

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
PLIST_NAME="com.tradingbot.broker"
PLIST_SRC="${PROJECT_DIR}/${PLIST_NAME}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${PLIST_NAME}.plist"
LOG_DIR="${PROJECT_DIR}/logs"

echo "=== IB Trading Bot Installer ==="
echo "Project: ${PROJECT_DIR}"
echo ""

# ---------------------------------------------------------------------------
# 1. Python version check
# ---------------------------------------------------------------------------
PYTHON_CMD=""
for cmd in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
            PYTHON_CMD="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    echo "ERROR: Python >= 3.10 is required but not found."
    echo "Install from https://www.python.org/downloads/"
    exit 1
fi

echo "[1/6] Python: $($PYTHON_CMD --version) ($PYTHON_CMD)"

# ---------------------------------------------------------------------------
# 2. Virtual environment
# ---------------------------------------------------------------------------
if [ ! -d "$VENV_DIR" ]; then
    echo "[2/6] Creating virtual environment..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
else
    echo "[2/6] Virtual environment already exists."
fi

# Activate
source "${VENV_DIR}/bin/activate"
echo "      Using: $(python3 --version) at $(which python3)"

# ---------------------------------------------------------------------------
# 3. Install dependencies
# ---------------------------------------------------------------------------
echo "[3/6] Installing dependencies..."
pip install --upgrade pip --quiet
pip install -r "${PROJECT_DIR}/requirements.txt" --quiet
echo "      Done. $(pip list --format=columns 2>/dev/null | wc -l | tr -d ' ') packages installed."

# ---------------------------------------------------------------------------
# 4. Initialise database
# ---------------------------------------------------------------------------
echo "[4/6] Initialising database..."
python3 -c "
from trading_bot.db.schema import create_tables
create_tables('${PROJECT_DIR}/trading_bot.db')
print('      Database ready.')
"

# ---------------------------------------------------------------------------
# 5. Create log directory
# ---------------------------------------------------------------------------
mkdir -p "$LOG_DIR"
echo "[5/6] Log directory: ${LOG_DIR}"

# ---------------------------------------------------------------------------
# 6. Install LaunchAgent
# ---------------------------------------------------------------------------
echo "[6/6] Installing LaunchAgent..."

# Generate the plist file
cat > "$PLIST_SRC" << PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PROJECT_DIR}/start_bot.sh</string>
    </array>

    <!-- Run Mon-Fri at 07:45 London time (before LSE 08:00 open).
         macOS cron uses local system time — adjust if your Mac is not on UK time. -->
    <key>StartCalendarInterval</key>
    <array>
        <!-- Monday -->
        <dict>
            <key>Weekday</key><integer>1</integer>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
        <!-- Tuesday -->
        <dict>
            <key>Weekday</key><integer>2</integer>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
        <!-- Wednesday -->
        <dict>
            <key>Weekday</key><integer>3</integer>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
        <!-- Thursday -->
        <dict>
            <key>Weekday</key><integer>4</integer>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
        <!-- Friday -->
        <dict>
            <key>Weekday</key><integer>5</integer>
            <key>Hour</key><integer>7</integer>
            <key>Minute</key><integer>45</integer>
        </dict>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/launchd_stdout.log</string>

    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/launchd_stderr.log</string>

    <!-- Don't relaunch if the bot exits cleanly (after wind-down). -->
    <key>KeepAlive</key>
    <false/>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${VENV_DIR}/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST_EOF

# Copy to LaunchAgents
mkdir -p "${HOME}/Library/LaunchAgents"
cp "$PLIST_SRC" "$PLIST_DST"

# Unload first (ignore errors if not loaded)
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "      LaunchAgent installed: ${PLIST_DST}"
echo "      Schedule: Mon-Fri at 07:45 local time"
echo ""

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo "=== Installation Complete ==="
echo ""
echo "  Config:      ${PROJECT_DIR}/config.yaml"
echo "  Database:    ${PROJECT_DIR}/trading_bot.db"
echo "  Logs:        ${LOG_DIR}/"
echo "  LaunchAgent: ${PLIST_DST}"
echo ""
echo "Next steps:"
echo "  1. Ensure IB Gateway is running (paper account U5433252, port 4001)"
echo "  2. Edit config.yaml if needed (Finnhub API key, ntfy topic, etc.)"
echo "  3. Test manually:  bash start_bot.sh"
echo "  4. The bot will auto-start Mon-Fri at 07:45"
echo ""
echo "To disable auto-start:"
echo "  launchctl unload ${PLIST_DST}"
echo ""
echo "To re-enable:"
echo "  launchctl load ${PLIST_DST}"

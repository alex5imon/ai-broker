"""Smoke test: exercise StateRecovery against the paper account.

Bypasses the operating-hours gate in main.py so we can validate the new
stale-cancel and EOD-flatten branches in isolation. Read-only by default —
the only side effects are (a) cancelling a genuinely stale entry order if
one happens to exist, and (b) submitting a flatten order ONLY if the wall
clock is past the configured EOD cutoff AND an intraday-tagged DB position
exists. Intended to be run interactively.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from trading_bot.config import Config
from trading_bot.constants import TZ_EASTERN
from trading_bot.gateway.connection import GatewayConnection
from trading_bot.gateway.recovery import StateRecovery
from trading_bot.notifications.notifier import Notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("smoke_recovery")


async def main() -> int:
    load_dotenv()
    config = Config.load("config.yaml")

    notifier = Notifier(config._raw)
    gw = GatewayConnection(config._raw, notifier)
    if not await gw.connect():
        logger.error("Could not connect to Alpaca paper")
        return 1

    db_path = "trading_bot/data/trading_bot.db"
    recovery = StateRecovery(
        gateway=gw,
        db_path=db_path,
        notifier=notifier,
        config=config._raw,
    )

    logger.info(
        "Running recovery — wall clock %s ET",
        datetime.now(tz=ZoneInfo("US/Eastern")).strftime("%H:%M:%S"),
    )
    result = await recovery.recover()
    print()
    print("=== Recovery result ===")
    print(result.summary())
    print()
    print("Stale orders cancelled:", result.stale_orders_cancelled)
    print("EOD flatten orders:    ", result.eod_flatten_orders)
    print("Has discrepancies:     ", result.has_discrepancies)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .alpaca_client import (
    build_client,
    get_positions_by_symbol,
    is_market_open,
    submit_market_order,
)
from .calendar import last_bar_timestamp, nyse_is_open
from .config import load_config
from .data import build_data_client, fetch_recent_bars
from .logging_setup import log_event, setup_logging
from .state import State
from .strategy import LOOKBACK_MINUTES, SYMBOLS, decide

STRATEGY_NAME = "default"
BAR_INTERVAL_MIN = 15


def main() -> int:
    cfg = load_config()
    now = datetime.now(timezone.utc)
    bar_ts = last_bar_timestamp(now, BAR_INTERVAL_MIN)
    run_tag = bar_ts.strftime("%Y%m%dT%H%M")

    logger = setup_logging(cfg.log_dir, run_tag)
    log_event(logger, "run_start", env=cfg.env, bar_ts=bar_ts.isoformat())

    if not nyse_is_open(now):
        log_event(logger, "market_closed_calendar")
        return 0

    client = build_client(cfg)
    if not is_market_open(client):
        log_event(logger, "market_closed_alpaca")
        return 0

    state_path = cfg.state_dir / f"{STRATEGY_NAME}.json"
    state = State.load(state_path)

    if state.last_bar_iso == bar_ts.isoformat():
        log_event(logger, "bar_already_processed", bar_ts=bar_ts.isoformat())
        return 0

    positions = get_positions_by_symbol(client)

    data_client = build_data_client(cfg)
    bars = fetch_recent_bars(
        data_client,
        symbols=SYMBOLS,
        lookback_minutes=LOOKBACK_MINUTES,
        bar_minutes=BAR_INTERVAL_MIN,
        now=now,
    )
    log_event(
        logger, "bars_fetched",
        symbols=SYMBOLS,
        counts={s: len(df) for s, df in bars.items()},
    )

    decisions = decide(
        strategy_name=STRATEGY_NAME,
        bar_ts=bar_ts,
        positions=positions,
        bars=bars,
    )
    log_event(logger, "decisions", count=len(decisions))

    for d in decisions:
        coid = d.client_order_id(STRATEGY_NAME, bar_ts)
        try:
            submit_market_order(
                client,
                symbol=d.symbol,
                qty=d.qty,
                side=d.side,
                client_order_id=coid,
            )
            log_event(
                logger, "order_submitted",
                symbol=d.symbol, side=d.side.value, qty=d.qty,
                client_order_id=coid, reason=d.reason,
            )
        except Exception as exc:
            log_event(
                logger, "order_failed",
                symbol=d.symbol, side=d.side.value, qty=d.qty,
                client_order_id=coid, error=str(exc),
            )

    state.last_bar_iso = bar_ts.isoformat()
    state.save(state_path)
    log_event(logger, "run_end")
    return 0


if __name__ == "__main__":
    sys.exit(main())

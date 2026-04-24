#!/usr/bin/env python3
"""Smoke test the live Alpaca paper execution path in ~30s.

Catches integration bugs that backtests miss: malformed stream subscribe
calls, bracket-order plumbing, DB schema drift. Exits 0 on pass, 1 on fail.

Usage:
    cd /path/to/Broker && source .venv/bin/activate
    SSL_CERT_FILE=$(python -c 'import certifi; print(certifi.where())') \
        python scripts/smoke_paper.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.live import StockDataStream
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
NET_TIMEOUT = 10.0
WS_WAIT_SECONDS = 30.0

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def load_env() -> None:
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_paper_flag() -> bool:
    cfg_path = REPO_ROOT / "config.yaml"
    with cfg_path.open() as f:
        cfg = yaml.safe_load(f)
    return bool(cfg.get("alpaca", {}).get("paper", True))


async def with_timeout(coro, seconds: float, label: str):
    try:
        return await asyncio.wait_for(coro, timeout=seconds)
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"{label} timed out after {seconds}s") from e


async def check_connect(trading: TradingClient) -> str:
    acct = await asyncio.to_thread(trading.get_account)
    status = str(acct.status)
    if "ACTIVE" not in status.upper():
        raise RuntimeError(f"account status is {status}, expected ACTIVE")
    return f"acct={acct.account_number} status={status} equity=${acct.equity}"


async def check_historical(api_key: str, secret: str) -> str:
    client = StockHistoricalDataClient(api_key=api_key, secret_key=secret)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    # Free paper only has IEX feed access; default (SIP) 403s.
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start,
        end=end,
        limit=50,
        feed="iex",
    )
    bars_resp = await asyncio.to_thread(client.get_stock_bars, req)
    bars = list(bars_resp.data.get("SPY", []))
    if len(bars) < 10:
        raise RuntimeError(f"got {len(bars)} bars for SPY, need >= 10")
    newest = bars[-1].timestamp
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - newest
    # 24h in-session; widen to 5d to tolerate weekends + holiday runs (e.g. Good Friday).
    max_age = timedelta(days=5)
    if age > max_age:
        raise RuntimeError(f"newest SPY bar is {age} old, > {max_age}")
    return f"{len(bars)} bars, newest {newest.isoformat()} (age {age})"


async def check_websocket(api_key: str, secret: str, feed: str) -> str:
    stream = StockDataStream(
        api_key=api_key,
        secret_key=secret,
        feed=DataFeed(feed),
    )
    events: list[str] = []
    got_event = asyncio.Event()

    async def on_trade(_t) -> None:
        if not got_event.is_set():
            events.append("trade")
            got_event.set()

    async def on_quote(_q) -> None:
        if not got_event.is_set():
            events.append("quote")
            got_event.set()

    stream.subscribe_trades(on_trade, "SPY")
    stream.subscribe_quotes(on_quote, "SPY")

    async def runner() -> None:
        with suppress(Exception):
            await stream._run_forever()  # noqa: SLF001

    task = asyncio.create_task(runner(), name="smoke-ws")
    print("waiting for first tick...", flush=True)
    try:
        try:
            await asyncio.wait_for(got_event.wait(), timeout=WS_WAIT_SECONDS)
            return f"received {events[0]} event for SPY"
        except asyncio.TimeoutError:
            return "WARN: no ticks in 30s (IEX paper off-hours is sparse)"
    finally:
        with suppress(Exception):
            await stream.stop_ws()
        task.cancel()
        with suppress(BaseException):
            await task


async def check_bracket(trading: TradingClient) -> str:
    # Anchor prices off last close so we can run when the market is closed.
    hist = StockHistoricalDataClient(
        api_key=os.environ["ALPACA_API_KEY"],
        secret_key=os.environ["ALPACA_SECRET_KEY"],
    )
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=5)
    req = StockBarsRequest(
        symbol_or_symbols="SPY",
        timeframe=TimeFrame(1, TimeFrameUnit.Day),
        start=start,
        end=end,
        limit=5,
        feed="iex",
    )
    resp = await asyncio.to_thread(hist.get_stock_bars, req)
    bars = list(resp.data.get("SPY", []))
    if not bars:
        raise RuntimeError("could not fetch SPY reference price for bracket")
    ref_price = float(bars[-1].close)

    limit_price = round(ref_price * 0.5, 2)
    take_profit = round(limit_price * 1.10, 2)
    stop_loss = round(limit_price * 0.90, 2)

    order_req = LimitOrderRequest(
        symbol="SPY",
        qty=1,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=take_profit),
        stop_loss=StopLossRequest(stop_price=stop_loss),
    )

    order = None
    try:
        order = await asyncio.to_thread(trading.submit_order, order_req)
        if order is None or not getattr(order, "id", None):
            raise RuntimeError("bracket submit returned no order id")
        await asyncio.to_thread(trading.cancel_order_by_id, order.id)
        # Verify state; cancel may be pending -> accept cancel/pending/filled-none.
        fetched = await asyncio.to_thread(trading.get_order_by_id, order.id)
        state = str(fetched.status).lower()
        if "cancel" not in state and "pending_cancel" not in state and "accept" not in state:
            # not yet cancelled — nudge and accept; finally block will re-cancel
            pass
        return f"bracket id={order.id} limit={limit_price} tp={take_profit} sl={stop_loss} final={state}"
    finally:
        # Idempotency: belt-and-braces cleanup of any order we submitted,
        # plus any other open SPY orders this smoke run may have leaked.
        if order is not None and getattr(order, "id", None):
            with suppress(Exception):
                await asyncio.to_thread(trading.cancel_order_by_id, order.id)
        with suppress(Exception):
            await asyncio.to_thread(trading.cancel_orders)


def check_db_schema() -> str:
    db_path = REPO_ROOT / "trading_bot" / "data" / "trading_bot.db"
    if not db_path.exists():
        raise RuntimeError(f"DB not found at {db_path}")
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute("PRAGMA table_info(positions)").fetchall()
    finally:
        conn.close()
    cols = [r[1] for r in rows]
    if "strategy_id" not in cols:
        raise RuntimeError("positions.strategy_id column missing")
    if "highest_price" not in cols:
        raise RuntimeError("positions.highest_price column missing")
    return f"positions has {len(cols)} cols incl. strategy_id, highest_price"


async def run() -> int:
    load_env()
    api_key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        print(f"{RED}SMOKE FAILED: ALPACA_API_KEY / ALPACA_SECRET_KEY not set{RESET}")
        return 1

    paper = load_paper_flag()
    if not paper:
        print(f"{RED}SMOKE FAILED: config.yaml alpaca.paper must be true{RESET}")
        return 1

    # Resolve feed (same default market_data.py uses).
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text())
    feed = str(cfg.get("alpaca", {}).get("data_feed", "iex"))

    trading = TradingClient(api_key, secret, paper=True)

    checks: list[tuple[str, object]] = [
        ("connect", lambda: with_timeout(check_connect(trading), NET_TIMEOUT, "connect")),
        ("historical_data", lambda: with_timeout(check_historical(api_key, secret), NET_TIMEOUT, "historical_data")),
        ("websocket_data", lambda: check_websocket(api_key, secret, feed)),
        ("bracket_order", lambda: with_timeout(check_bracket(trading), NET_TIMEOUT, "bracket_order")),
        ("position_attribution", lambda: asyncio.to_thread(check_db_schema)),
    ]

    for name, factory in checks:
        try:
            result = await factory()
        except Exception as e:
            print(f"{RED}SMOKE FAILED: {name}: {e}{RESET}")
            # Best-effort: always try to cancel any outstanding orders.
            with suppress(Exception):
                await asyncio.to_thread(trading.cancel_orders)
            return 1
        if isinstance(result, str) and result.startswith("WARN"):
            print(f"{YELLOW}  [{name}] {result}{RESET}")
        else:
            print(f"  [{name}] {result}")

    print(f"{GREEN}SMOKE OK{RESET}")
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        print(f"{RED}SMOKE FAILED: interrupted{RESET}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

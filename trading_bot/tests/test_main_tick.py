"""Integration tests for TradingBot.tick() — the per-tick orchestrator.

Each test constructs a real TradingBot with the project config, then patches
the gateway, market data, and time-window helpers so we can drive the bot
through a single deterministic tick without hitting Alpaca."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from trading_bot.constants import Market, TZ_EASTERN
from trading_bot.main import TradingBot, _parse_time

pytestmark = pytest.mark.critical

ET = TZ_EASTERN


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trading_bot(config, tmp_db_path, monkeypatch):
    """Real TradingBot with all external IO replaced by mocks.

    The instance has been instrumented so calling .tick() exercises the
    full orchestrator without hitting the network or any global handlers.
    """
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    # Mutate the existing `database.path` key — `Config.db_path` reads
    # `_raw["database"]["path"]`. Earlier code wrote `_raw["db"]` (wrong
    # key) so the bot silently used the persistent dev DB and tests
    # leaked state into each other.
    config._raw["database"]["path"] = tmp_db_path

    bot = TradingBot(config, mode="normal", dry_run=False)

    # --- Replace external dependencies with awaitable mocks ---
    bot._gateway = MagicMock()
    bot._gateway.connect = AsyncMock(return_value=True)
    bot._gateway.disconnect = AsyncMock(return_value=None)
    bot._gateway.is_connected = True
    bot._gateway.get_account_summary = AsyncMock(return_value={
        "NetLiquidation": "1000.0", "SettledCash": "1000.0",
        "BuyingPower": "1000.0",
    })
    bot._gateway.get_positions = AsyncMock(return_value=[])
    bot._gateway.get_open_orders = AsyncMock(return_value=[])

    bot._market_data = MagicMock()
    bot._market_data.refresh_quotes = AsyncMock()
    bot._market_data.get_latest_price = MagicMock(return_value=100.0)
    bot._market_data.is_stale = MagicMock(return_value=False)
    bot._market_data.trading_paused = False
    bot._market_data.subscribe = AsyncMock()
    bot._market_data.get_historical_bars = AsyncMock(return_value=[])

    bot._sentiment = MagicMock()
    bot._sentiment.get_sentiment = AsyncMock(return_value=0.2)
    bot._sentiment.refresh_all = AsyncMock()  # bulk refresh from pre_market_scan

    bot._earnings = MagicMock()
    bot._earnings.is_in_blackout = MagicMock(return_value=False)
    bot._earnings.refresh = AsyncMock()

    bot._notifier = MagicMock()
    bot._notifier.send = AsyncMock()
    bot._notifier.send_sync = MagicMock()
    bot._notifier.shutdown = AsyncMock()
    bot._notifier.trade_entry = AsyncMock()
    bot._notifier.position_closed = AsyncMock()
    bot._notifier.bot_startup = AsyncMock()
    bot._notifier.daily_summary = AsyncMock()
    bot._notifier.phase_transition = AsyncMock()
    bot._notifier.gateway_alert = AsyncMock()
    bot._notifier.kill_switch = AsyncMock()
    bot._notifier.drawdown_alert = AsyncMock()

    bot._state_recovery = MagicMock()
    recovery_result = MagicMock()
    recovery_result.summary = MagicMock(return_value="ok")
    bot._state_recovery.recover = AsyncMock(return_value=recovery_result)

    bot._order_manager._check_order_statuses = AsyncMock()

    if bot._strategy_manager is not None:
        bot._strategy_manager.scan_for_entries = AsyncMock(return_value=0)
        bot._strategy_manager.check_exits = AsyncMock(return_value=0)
        bot._strategy_manager.get_comparison_report = MagicMock(return_value={})

    return bot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _override_hours(bot: TradingBot, *, start: str, end: str) -> None:
    """Override only the operating-hour config keys; leave others intact."""
    real_get = bot._config._get

    def _patched(*args, **kwargs):
        if len(args) >= 2 and args[0] == "schedule":
            if args[1] == "bot_start_gmt":
                return start
            if args[1] == "bot_end_gmt":
                return end
        return real_get(*args, **kwargs)

    bot._config._get = MagicMock(side_effect=_patched)


def _open_hours(bot: TradingBot) -> None:
    """Open the operating window so the tick proceeds past the time gate."""
    _override_hours(bot, start="00:00", end="23:59")


# ---------------------------------------------------------------------------
# Trading-day gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_exits_on_non_trading_day(trading_bot):
    """When today is not a trading day the tick exits before any IO."""
    # Force is_trading_day to return False
    trading_bot._config.is_trading_day = MagicMock(return_value=False)
    await trading_bot.tick()
    # Gateway never connected
    trading_bot._gateway.connect.assert_not_called()


@pytest.mark.asyncio
async def test_tick_exits_outside_operating_hours(trading_bot):
    """Tick exits when current UTC time is outside the operating window."""
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    # Force the window to a 1-min slot in the past
    _override_hours(trading_bot, start="00:00", end="00:01")
    await trading_bot.tick()
    trading_bot._gateway.connect.assert_not_called()


@pytest.mark.asyncio
async def test_tick_aborts_on_gateway_failure(trading_bot):
    """When Alpaca connect fails, tick aborts before reconcile/orders."""
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    # Wide-open hours
    _open_hours(trading_bot)
    trading_bot._gateway.connect = AsyncMock(return_value=False)
    await trading_bot.tick()
    # State recovery should NOT have been called
    trading_bot._state_recovery.recover.assert_not_called()


# ---------------------------------------------------------------------------
# Happy-path tick
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_full_cycle_outside_market_window(trading_bot):
    """Tick runs all infrastructure but skips entry/wind-down outside windows."""
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    # Open hours + force all market window helpers to False
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=False)

    await trading_bot.tick()

    trading_bot._gateway.connect.assert_called_once()
    trading_bot._state_recovery.recover.assert_awaited()
    trading_bot._order_manager._check_order_statuses.assert_awaited()
    trading_bot._market_data.refresh_quotes.assert_awaited()
    # No entry scan (market closed)
    if trading_bot._strategy_manager is not None:
        trading_bot._strategy_manager.scan_for_entries.assert_not_called()


@pytest.mark.asyncio
async def test_tick_runs_entry_scan_when_in_execution_window(trading_bot):
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=True)
    trading_bot._is_market_in_winddown = MagicMock(return_value=False)

    await trading_bot.tick()

    if trading_bot._strategy_manager is not None:
        trading_bot._strategy_manager.scan_for_entries.assert_awaited()


@pytest.mark.asyncio
async def test_tick_skips_entry_in_close_only_mode(trading_bot):
    trading_bot._mode = "close-only"
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=True)
    trading_bot._is_market_in_winddown = MagicMock(return_value=False)

    await trading_bot.tick()

    if trading_bot._strategy_manager is not None:
        trading_bot._strategy_manager.scan_for_entries.assert_not_called()


@pytest.mark.asyncio
async def test_tick_runs_wind_down_within_window(trading_bot):
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=True)
    trading_bot.wind_down = AsyncMock()
    await trading_bot.tick()
    trading_bot.wind_down.assert_awaited_with(Market.US)


@pytest.mark.asyncio
async def test_tick_disconnects_in_finally_even_on_error(trading_bot):
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    # Force state_recovery to raise — the outer try/finally should still
    # disconnect cleanly even when an inner step blows up.
    trading_bot._state_recovery.recover = AsyncMock(
        side_effect=RuntimeError("boom")
    )
    await trading_bot.tick()
    trading_bot._gateway.disconnect.assert_awaited()
    trading_bot._notifier.shutdown.assert_awaited()


# ---------------------------------------------------------------------------
# Pre-market scan
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pre_market_scan_calls_bulk_refresh(trading_bot):
    """pre_market_scan must hit the real bulk-refresh APIs (not removed
    per-ticker stubs that previously failed silently)."""
    await trading_bot.pre_market_scan(Market.US)
    trading_bot._earnings.refresh.assert_awaited_once()
    trading_bot._sentiment.refresh_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_market_scan_empty_watchlist_skips(trading_bot):
    trading_bot._config.get_watchlist = MagicMock(return_value=[])
    await trading_bot.pre_market_scan(Market.US)
    trading_bot._earnings.refresh.assert_not_called()
    trading_bot._sentiment.refresh_all.assert_not_called()


@pytest.mark.asyncio
async def test_pre_market_scan_swallows_refresh_errors(trading_bot):
    trading_bot._earnings.refresh = AsyncMock(side_effect=RuntimeError("api down"))
    trading_bot._sentiment.refresh_all = AsyncMock(side_effect=RuntimeError("api down"))
    # Must not raise — refresh failures are non-fatal
    await trading_bot.pre_market_scan(Market.US)


@pytest.mark.asyncio
async def test_failed_pre_market_scan_does_not_latch_done_flag(trading_bot):
    """Regression: a failed pre-market scan must NOT latch
    ``pre_market_done=True`` — otherwise every subsequent tick today
    would skip the retry and the watchlist would be frozen with
    whatever stale state existed when the scan blew up.

    Item 6 of risk_infrastructure_gaps.md: the flag write was sitting
    after the try/except and ran unconditionally, including when the
    scan raised. Now it lives inside the try and only fires on success.

    Tracks flag state via ``_save_day_flags`` patches rather than
    re-reading the DB — keeps the assertion focused on whether the
    flag was *written* in this tick, regardless of any prior state.
    """
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=True)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=False)
    # Force-clear any prior flag state so the pre-market branch runs
    # (otherwise the fixture's persistent DB short-circuits us).
    trading_bot._load_day_flags = MagicMock(return_value={})
    saved_flags_failed: list[dict[str, Any]] = []
    trading_bot._save_day_flags = MagicMock(
        side_effect=lambda _today, flags: saved_flags_failed.append(dict(flags)),
    )
    # Pre-market scan blows up.
    trading_bot.pre_market_scan = AsyncMock(side_effect=RuntimeError("scan boom"))

    await trading_bot.tick()

    # No save in the try block means saved_flags carries only writes
    # from the after-close handlers — and none of them should contain
    # ``pre_market_done=True`` because nothing in this tick set it.
    # Check for the dangerous state — `pre_market_done == True` saved
    # to disk — rather than the looser "key absent" form. A future
    # refactor that pre-initialises `flags["pre_market_done"] = False`
    # before saving would still be safe but would fail the looser test.
    pre_market_writes = [
        f for f in saved_flags_failed if f.get("pre_market_done") is True
    ]
    assert pre_market_writes == [], (
        f"Failed scan latched pre_market_done: {pre_market_writes}"
    )


@pytest.mark.asyncio
async def test_successful_pre_market_scan_latches_done_flag(trading_bot):
    """Sanity: the happy path still latches the flag so we don't
    re-run the scan every 5-min tick for the rest of the day."""
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=True)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=False)
    trading_bot._load_day_flags = MagicMock(return_value={})
    saved_flags_ok: list[dict[str, Any]] = []
    trading_bot._save_day_flags = MagicMock(
        side_effect=lambda _today, flags: saved_flags_ok.append(dict(flags)),
    )
    trading_bot.pre_market_scan = AsyncMock(return_value=None)

    await trading_bot.tick()

    pre_market_writes = [f for f in saved_flags_ok if f.get("pre_market_done")]
    assert pre_market_writes, "Successful scan must latch pre_market_done"


@pytest.mark.asyncio
async def test_failed_wind_down_does_not_latch_done_flag(trading_bot):
    """Sibling of item 6: a failed `wind_down` must NOT latch
    `wind_down_done=True`. Latching on failure is dangerous here —
    wind-down closes intraday positions before the close, and a
    permanently-suppressed retry leaves them open overnight.
    """
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=True)
    trading_bot._load_day_flags = MagicMock(return_value={})
    saved_flags_failed: list[dict[str, Any]] = []
    trading_bot._save_day_flags = MagicMock(
        side_effect=lambda _today, flags: saved_flags_failed.append(dict(flags)),
    )
    trading_bot.wind_down = AsyncMock(side_effect=RuntimeError("flatten failed"))

    await trading_bot.tick()

    wind_down_writes = [
        f for f in saved_flags_failed if f.get("wind_down_done") is True
    ]
    assert wind_down_writes == [], (
        f"Failed wind-down latched wind_down_done: {wind_down_writes}"
    )


@pytest.mark.asyncio
async def test_successful_wind_down_latches_done_flag(trading_bot):
    """Happy path: successful wind-down latches the flag so we don't
    re-flatten on every tick in the wind-down window."""
    trading_bot._config.is_trading_day = MagicMock(return_value=True)
    _open_hours(trading_bot)
    trading_bot._is_market_in_premarket = MagicMock(return_value=False)
    trading_bot._is_market_in_execution = MagicMock(return_value=False)
    trading_bot._is_market_in_winddown = MagicMock(return_value=True)
    trading_bot._load_day_flags = MagicMock(return_value={})
    saved_flags_ok: list[dict[str, Any]] = []
    trading_bot._save_day_flags = MagicMock(
        side_effect=lambda _today, flags: saved_flags_ok.append(dict(flags)),
    )
    trading_bot.wind_down = AsyncMock(return_value=None)

    await trading_bot.tick()

    wind_down_writes = [f for f in saved_flags_ok if f.get("wind_down_done")]
    assert wind_down_writes, "Successful wind-down must latch wind_down_done"


# ---------------------------------------------------------------------------
# Day-flag persistence
# ---------------------------------------------------------------------------


def test_load_save_day_flags_round_trip(trading_bot):
    today = date(2026, 4, 27)
    trading_bot._save_day_flags(today, {"pre_market_done": True, "x": 1})
    loaded = trading_bot._load_day_flags(today)
    assert loaded.get("pre_market_done") is True
    assert loaded.get("x") == 1


def test_load_day_flags_returns_empty_when_stale(trading_bot):
    """Flags from an earlier date should not leak into today's flags."""
    yesterday = date(2026, 4, 26)
    today = date(2026, 4, 27)
    trading_bot._save_day_flags(yesterday, {"pre_market_done": True})
    loaded = trading_bot._load_day_flags(today)
    assert loaded == {}


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------


def test_market_window_helpers_callable(trading_bot):
    """Exercise the time-window helpers — values depend on wall-clock time
    but the helpers themselves should not raise."""
    for fn in (
        trading_bot._is_market_in_premarket,
        trading_bot._is_market_in_execution,
        trading_bot._is_market_in_winddown,
    ):
        out = fn(Market.US)
        assert isinstance(out, bool)


def test_parse_time_helper():
    t = _parse_time("09:30")
    assert t.hour == 9
    assert t.minute == 30


# ---------------------------------------------------------------------------
# Dry-run flag visibility
# ---------------------------------------------------------------------------


def test_dry_run_flag_propagates_to_strategy_manager(config, tmp_db_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    config._raw["database"]["path"] = tmp_db_path
    bot = TradingBot(config, mode="normal", dry_run=True)
    if bot._strategy_manager is not None:
        assert bot._strategy_manager._dry_run is True


def test_construct_with_close_only_mode(config, tmp_db_path, monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    config._raw["database"]["path"] = tmp_db_path
    bot = TradingBot(config, mode="close-only", dry_run=False)
    assert bot._mode == "close-only"

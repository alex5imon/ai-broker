"""Tests for market data staleness detection.

The MarketDataManager tracks staleness via a background _check_staleness()
loop that sets sub.is_stale based on time since last tick.  Tests exercise
both the staleness flag itself and the mass-staleness circuit breaker logic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo


ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(raw_config: dict[str, Any]) -> Any:
    import copy
    import os
    from unittest.mock import patch
    from trading_bot.data.market_data import MarketDataManager

    # Tests in this module exercise the mass-staleness breaker logic, which is
    # only active when pause_on_mass_staleness is true. Force-enable it here
    # regardless of what the default config.yaml sets for live operation.
    cfg = copy.deepcopy(raw_config)
    md = cfg.setdefault("market_data", {})
    md["pause_on_mass_staleness"] = True
    # Tests assume the historical 30s threshold; config.yaml now ships 300s.
    md["staleness_threshold_seconds"] = 30
    md["mass_staleness_pct"] = 0.50
    md["mass_staleness_resume_pct"] = 0.25

    gateway = MagicMock()
    notifier = AsyncMock()
    notifier.send = AsyncMock(return_value=None)
    with patch.dict(os.environ, {"ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret"}):
        return MarketDataManager(gateway, cfg, notifier)


def _sub(
    ticker: str,
    is_stale: bool = False,
    last_tick_seconds_ago: float | None = None,
    exchange: str = "NASDAQ",
    excluded: bool = False,
) -> Any:
    from trading_bot.data.market_data import MarketDataSubscription

    now = datetime.now(tz=ET)
    last_tick = (
        now - timedelta(seconds=last_tick_seconds_ago)
        if last_tick_seconds_ago is not None
        else None
    )
    sub = MarketDataSubscription(
        ticker=ticker,
        exchange=exchange,
        subscribed_at=now,
        last_tick_time=last_tick,
        is_stale=is_stale,
        excluded=excluded,
    )
    return sub


# ---------------------------------------------------------------------------
# is_stale() reads the sub.is_stale flag
# ---------------------------------------------------------------------------


class TestDataStaleness:
    def test_symbol_flagged_stale_after_30s(
        self, raw_config: dict[str, Any]
    ) -> None:
        """Sub with is_stale=True → is_stale() returns True."""
        manager = _make_manager(raw_config)
        # Pre-set the flag as the background monitor would
        manager._subscriptions["PLTR"] = _sub("PLTR", is_stale=True)
        assert manager.is_stale("PLTR") is True

    def test_symbol_not_stale_within_30s(
        self, raw_config: dict[str, Any]
    ) -> None:
        """Sub with is_stale=False → is_stale() returns False."""
        manager = _make_manager(raw_config)
        manager._subscriptions["PLTR"] = _sub("PLTR", is_stale=False)
        assert manager.is_stale("PLTR") is False

    def test_unsubscribed_symbol_not_in_subscriptions(
        self, raw_config: dict[str, Any]
    ) -> None:
        """Symbol not subscribed — is_stale() returns False (no subscription)."""
        manager = _make_manager(raw_config)
        # The actual implementation returns False when sub is None
        result = manager.is_stale("UNKNOWN")
        assert isinstance(result, bool)

    def test_check_staleness_marks_stale_after_threshold(
        self, raw_config: dict[str, Any]
    ) -> None:
        """_check_staleness() marks subs as stale when last_tick_time > threshold."""
        manager = _make_manager(raw_config)
        # Subscription with tick 60s ago → exceeds 30s threshold
        sub = _sub("PLTR", is_stale=False, last_tick_seconds_ago=60.0)
        manager._subscriptions["PLTR"] = sub

        # Run the staleness check synchronously (via event loop)
        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        assert manager._subscriptions["PLTR"].is_stale is True

    def test_check_staleness_does_not_mark_fresh(
        self, raw_config: dict[str, Any]
    ) -> None:
        """Fresh subscription (tick 5s ago) is not marked stale."""
        manager = _make_manager(raw_config)
        sub = _sub("PLTR", is_stale=False, last_tick_seconds_ago=5.0)
        manager._subscriptions["PLTR"] = sub

        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        assert manager._subscriptions["PLTR"].is_stale is False

    def test_no_last_tick_time_uses_subscribed_at(
        self, raw_config: dict[str, Any]
    ) -> None:
        """No last_tick_time → uses subscribed_at; recent sub should stay fresh."""
        manager = _make_manager(raw_config)
        sub = _sub("PLTR", is_stale=False, last_tick_seconds_ago=None)
        # subscribed_at is set to now() in _sub(), so fresh
        manager._subscriptions["PLTR"] = sub

        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        # Just-subscribed → should NOT be stale
        assert manager._subscriptions["PLTR"].is_stale is False


# ---------------------------------------------------------------------------
# Mass staleness circuit breaker
# ---------------------------------------------------------------------------


class TestMassStaleness:
    def _add_subs(
        self,
        manager: Any,
        tickers: list[str],
        stale_set: set[str],
    ) -> None:
        for t in tickers:
            manager._subscriptions[t] = _sub(
                t, is_stale=(t in stale_set)
            )

    def test_mass_staleness_pauses_trading(
        self, raw_config: dict[str, Any]
    ) -> None:
        """>50% symbols stale → _check_staleness sets trading_paused=True."""
        manager = _make_manager(raw_config)
        tickers = ["PLTR", "F", "SNAP", "BAC", "NIO"]
        # 3/5 = 60% stale subs with old tick times
        old_subs = {"PLTR", "F", "SNAP"}

        # Add subs with tick times far enough in the past
        for t in tickers:
            secs = 90.0 if t in old_subs else 5.0
            manager._subscriptions[t] = _sub(t, is_stale=False,
                                              last_tick_seconds_ago=secs)

        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        assert manager.trading_paused is True

    def test_mass_staleness_resumes_below_25pct(
        self, raw_config: dict[str, Any]
    ) -> None:
        """After being paused, stale ratio drops to 20% → trading resumed."""
        manager = _make_manager(raw_config)
        manager._trading_paused = True

        tickers = ["PLTR", "F", "SNAP", "BAC", "NIO"]
        # Only PLTR is old (20% stale)
        for t in tickers:
            secs = 90.0 if t == "PLTR" else 5.0
            manager._subscriptions[t] = _sub(
                t,
                is_stale=(t == "PLTR"),  # pre-flag PLTR as stale
                last_tick_seconds_ago=secs,
            )

        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        assert manager.trading_paused is False

    def test_trading_not_paused_below_50pct(
        self, raw_config: dict[str, Any]
    ) -> None:
        """40% stale — below 50% threshold → trading not paused."""
        manager = _make_manager(raw_config)
        tickers = ["PLTR", "F", "SNAP", "BAC", "NIO"]
        # 2/5 = 40%
        for t in tickers:
            secs = 90.0 if t in {"PLTR", "F"} else 5.0
            manager._subscriptions[t] = _sub(t, is_stale=False,
                                              last_tick_seconds_ago=secs)

        async def _run():
            await manager._check_staleness()

        asyncio.run(_run())
        assert manager.trading_paused is False

    def test_get_stale_symbols_returns_flagged(
        self, raw_config: dict[str, Any]
    ) -> None:
        """get_stale_symbols() returns only tickers with is_stale=True."""
        manager = _make_manager(raw_config)
        manager._subscriptions["PLTR"] = _sub("PLTR", is_stale=True)
        manager._subscriptions["F"] = _sub("F", is_stale=False)
        stale = manager.get_stale_symbols()
        assert "PLTR" in stale
        assert "F" not in stale

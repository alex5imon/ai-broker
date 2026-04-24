"""Regression tests for GatewayConnection reconnect notification logic.

Locks in two behaviours after the 2026-04-24 fix:

1. Transient outages (under the configured threshold) must NOT fire
   the "connection lost" / "reconnected" ntfy pair — only logs.
2. Extended outages above the threshold MUST fire both alerts, and the
   reported downtime must be the real elapsed time, not 0 seconds.
   (The previous bug was that ``connect()`` clears ``_disconnect_time``
   on success, which zeroed the reported downtime in the caller.)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def gateway_config() -> dict[str, object]:
    """Minimal Alpaca-section config for a GatewayConnection in tests.

    Uses ``retry_backoff_seconds=0`` so the retry loop doesn't actually
    sleep, and flips the alert threshold per-test.
    """
    return {
        "alpaca": {
            "paper": True,
            "max_retries": 3,
            "retry_backoff_seconds": 0,
            "reconnect_alert_threshold_seconds": 60,
        }
    }


@pytest.fixture
def fake_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply Alpaca env vars so connect() doesn't early-exit."""
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")


def _build_gateway(config: dict[str, object], notifier: AsyncMock):
    from trading_bot.gateway.connection import GatewayConnection
    return GatewayConnection(config=config, notifier=notifier)


@pytest.mark.asyncio
async def test_short_outage_is_silent(
    gateway_config: dict[str, object],
    mock_notifier: AsyncMock,
    fake_env: None,
) -> None:
    """Outage under threshold: no ntfy alert fires, only log entries."""
    gw = _build_gateway(gateway_config, mock_notifier)

    # Simulate a live connection then a transient blip: first connect()
    # succeeds quickly, reconnect also succeeds on attempt 1.
    async def fake_connect() -> bool:
        # connect() in production sets _disconnect_time=None on success.
        gw._disconnect_time = None
        gw._connected = True
        return True

    # Freeze time so monotonic stays under the 60s threshold.
    with patch.object(gw, "connect", side_effect=fake_connect), \
         patch("trading_bot.gateway.connection.time.monotonic", return_value=1000.0):
        # Seed a disconnect at t=1000; reconnect at t=1000 (same tick)
        gw._disconnect_time = 1000.0
        await gw._handle_disconnect()

    # No ntfy should have fired for a sub-threshold outage
    mock_notifier.gateway_alert.assert_not_called()


@pytest.mark.asyncio
async def test_long_outage_alerts_and_reports_real_downtime(
    gateway_config: dict[str, object],
    mock_notifier: AsyncMock,
    fake_env: None,
) -> None:
    """Outage above threshold: lost + reconnected ntfy fire with real downtime."""
    gw = _build_gateway(gateway_config, mock_notifier)

    # Mock connect() to mutate state like the real one does.
    async def fake_connect() -> bool:
        gw._disconnect_time = None  # Production clears this on success.
        gw._connected = True
        return True

    # Advance time by 300s between disconnect and reconnect so we blow
    # past the 60s threshold. Patch monotonic to return increasing values.
    times = iter([1000.0, 1300.0, 1300.0, 1300.0, 1300.0])
    with patch.object(gw, "connect", side_effect=fake_connect), \
         patch(
             "trading_bot.gateway.connection.time.monotonic",
             side_effect=lambda: next(times, 1300.0),
         ):
        gw._disconnect_time = 1000.0
        await gw._handle_disconnect()

    # Both alerts should have fired: one "lost", one "reconnected"
    assert mock_notifier.gateway_alert.await_count == 2

    messages = [call.args[0] for call in mock_notifier.gateway_alert.await_args_list]
    assert any("connection lost" in m.lower() for m in messages), messages
    # Critical fix: downtime must be real (~300s), not 0s
    assert any("300s downtime" in m for m in messages), (
        f"Expected '300s downtime' in ntfy messages, got {messages!r}. "
        "Downtime calc likely broken again — see _handle_disconnect."
    )


@pytest.mark.asyncio
async def test_exhausted_retries_always_alerts_even_for_short_outage(
    gateway_config: dict[str, object],
    mock_notifier: AsyncMock,
    fake_env: None,
) -> None:
    """Exhausted retries fire a CRITICAL alert regardless of threshold."""
    gw = _build_gateway(gateway_config, mock_notifier)

    # Force every reconnect attempt to fail
    async def fake_connect_fail() -> bool:
        return False

    # Make _connection_wait_loop succeed on first tick so the test terminates
    async def fake_wait_loop() -> None:
        return None

    with patch.object(gw, "connect", side_effect=fake_connect_fail), \
         patch.object(gw, "_connection_wait_loop", side_effect=fake_wait_loop), \
         patch("trading_bot.gateway.connection.time.monotonic", return_value=1000.0):
        gw._disconnect_time = 1000.0
        await gw._handle_disconnect()

    # Should have fired the CRITICAL exhausted-retries alert
    critical_calls = [
        call for call in mock_notifier.gateway_alert.await_args_list
        if call.kwargs.get("is_critical") is True
    ]
    assert len(critical_calls) == 1, (
        f"Expected one CRITICAL alert on exhausted retries, got "
        f"{mock_notifier.gateway_alert.await_args_list!r}"
    )
    assert "attempts failed" in critical_calls[0].args[0]

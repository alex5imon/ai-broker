"""Tests for env credential resolution and the Alpaca gateway connection."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.env import resolve_alpaca_env
from trading_bot.gateway.connection import GatewayConnection


# ---------------------------------------------------------------------------
# env.resolve_alpaca_env
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for var in (
        "ALPACA_ENV",
        "ALPACA_API_KEY",
        "ALPACA_SECRET_KEY",
        "ALPACA_PAPER_KEY_ID",
        "ALPACA_PAPER_SECRET",
        "ALPACA_LIVE_KEY_ID",
        "ALPACA_LIVE_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_resolve_alpaca_env_paper_default(monkeypatch):
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "pk1")
    monkeypatch.setenv("ALPACA_PAPER_SECRET", "ps1")
    key, secret, is_paper = resolve_alpaca_env()
    assert key == "pk1"
    assert secret == "ps1"
    assert is_paper is True
    # Side effect: legacy names exported
    assert os.environ.get("ALPACA_API_KEY") == "pk1"
    assert os.environ.get("ALPACA_SECRET_KEY") == "ps1"


def test_resolve_alpaca_env_live_uses_live_pair(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "live")
    monkeypatch.setenv("ALPACA_LIVE_KEY_ID", "lk1")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "ls1")
    key, secret, is_paper = resolve_alpaca_env()
    assert (key, secret, is_paper) == ("lk1", "ls1", False)


def test_resolve_alpaca_env_existing_legacy_keys_short_circuit(monkeypatch):
    """If ALPACA_API_KEY/SECRET_KEY are already set, those win."""
    monkeypatch.setenv("ALPACA_API_KEY", "preset-k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "preset-s")
    monkeypatch.setenv("ALPACA_PAPER_KEY_ID", "should-be-ignored")
    key, secret, _ = resolve_alpaca_env()
    assert (key, secret) == ("preset-k", "preset-s")


def test_resolve_alpaca_env_missing_returns_empty(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "paper")
    # Patch load_dotenv to a no-op so the project's real .env doesn't leak in.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    key, secret, is_paper = resolve_alpaca_env()
    assert (key, secret, is_paper) == ("", "", True)


def test_resolve_alpaca_env_strips_whitespace_and_lowercases(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "  LIVE  ")
    monkeypatch.setenv("ALPACA_LIVE_KEY_ID", "x")
    monkeypatch.setenv("ALPACA_LIVE_SECRET", "y")
    _, _, is_paper = resolve_alpaca_env()
    assert is_paper is False


# ---------------------------------------------------------------------------
# GatewayConnection
# ---------------------------------------------------------------------------


def _make_account(account_number="A1", equity="1000.0", cash="800.0",
                  buying_power="800.0", status="ACTIVE"):
    a = MagicMock()
    a.account_number = account_number
    a.equity = equity
    a.cash = cash
    a.buying_power = buying_power
    a.status = status
    return a


@pytest.mark.asyncio
async def test_gateway_connect_paper_uses_env(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "paper")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    notifier = MagicMock()
    gw = GatewayConnection({"alpaca": {}}, notifier)

    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        ok = await gw.connect()
    assert ok is True
    assert gw.is_connected is True
    assert gw.account_id == "A1"
    # paper=True passed to TradingClient
    call_kwargs = MockClient.call_args.kwargs
    assert call_kwargs["paper"] is True


@pytest.mark.asyncio
async def test_gateway_connect_live_when_env_set(monkeypatch):
    monkeypatch.setenv("ALPACA_ENV", "live")
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        MockClient.return_value.get_account.return_value = _make_account()
        await gw.connect()
    assert MockClient.call_args.kwargs["paper"] is False


@pytest.mark.asyncio
async def test_gateway_connect_falls_back_to_config_when_env_unset(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    # No ALPACA_ENV → uses config.alpaca.paper
    gw = GatewayConnection({"alpaca": {"paper": False}}, MagicMock())
    assert gw._paper is False


@pytest.mark.asyncio
async def test_gateway_missing_credentials_returns_false():
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    ok = await gw.connect()
    assert ok is False
    assert gw.is_connected is False


@pytest.mark.asyncio
async def test_gateway_connect_failure_returns_false(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        MockClient.side_effect = RuntimeError("api down")
        ok = await gw.connect()
    assert ok is False
    assert gw.is_connected is False


@pytest.mark.asyncio
async def test_client_property_raises_when_disconnected():
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with pytest.raises(RuntimeError):
        _ = gw.client


@pytest.mark.asyncio
async def test_disconnect_clears_state(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        MockClient.return_value.get_account.return_value = _make_account()
        await gw.connect()
    assert gw.is_connected
    await gw.disconnect()
    assert gw.is_connected is False
    with pytest.raises(RuntimeError):
        _ = gw.client


@pytest.mark.asyncio
async def test_get_account_summary_returns_dict(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account(
            equity="123.45", cash="99.0", buying_power="99.0",
        )
        await gw.connect()
        summary = await gw.get_account_summary()
    assert summary["NetLiquidation"] == "123.45"
    assert summary["TotalCashValue"] == "99.0"
    assert summary["BuyingPower"] == "99.0"


@pytest.mark.asyncio
async def test_get_account_summary_when_disconnected_returns_empty():
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    summary = await gw.get_account_summary()
    assert summary == {}


@pytest.mark.asyncio
async def test_get_account_summary_swallows_errors(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        await gw.connect()
        # Now break get_account
        instance.get_account.side_effect = RuntimeError("boom")
        summary = await gw.get_account_summary()
    assert summary == {}


@pytest.mark.asyncio
async def test_get_positions_when_disconnected_returns_empty():
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    assert await gw.get_positions() == []


@pytest.mark.asyncio
async def test_get_positions_returns_list(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        instance.get_all_positions.return_value = ["pos1", "pos2"]
        await gw.connect()
        positions = await gw.get_positions()
    assert positions == ["pos1", "pos2"]


@pytest.mark.asyncio
async def test_get_positions_swallows_errors(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        instance.get_all_positions.side_effect = RuntimeError("nope")
        await gw.connect()
        assert await gw.get_positions() == []


@pytest.mark.asyncio
async def test_get_open_orders_returns_list(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        instance.get_orders.return_value = ["o1"]
        await gw.connect()
        orders = await gw.get_open_orders()
    assert orders == ["o1"]


@pytest.mark.asyncio
async def test_get_open_orders_swallows_errors(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    with patch("trading_bot.gateway.connection.TradingClient") as MockClient:
        instance = MockClient.return_value
        instance.get_account.return_value = _make_account()
        instance.get_orders.side_effect = RuntimeError("nope")
        await gw.connect()
        assert await gw.get_open_orders() == []


@pytest.mark.asyncio
async def test_get_open_orders_when_disconnected_returns_empty():
    gw = GatewayConnection({"alpaca": {}}, MagicMock())
    assert await gw.get_open_orders() == []

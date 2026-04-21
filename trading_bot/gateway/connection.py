"""Alpaca Trading API connection manager with health monitoring."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import TradeAccount

from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)


class GatewayConnection:
    """Manages the connection to Alpaca Trading API.

    Unlike IB Gateway, Alpaca uses a REST API — there is no persistent
    socket connection.  "Connected" means the API keys are valid and
    we can reach the service.  Health checks poll the account endpoint.
    """

    def __init__(self, config: dict[str, Any], notifier: Notifier) -> None:
        alpaca_cfg: dict[str, Any] = config.get("alpaca", {})

        self._api_key: str = os.environ.get("ALPACA_API_KEY", "")
        self._secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")
        self._paper: bool = bool(alpaca_cfg.get("paper", False))
        self._max_retries: int = int(alpaca_cfg.get("max_retries", 5))
        self._retry_backoff: int = int(alpaca_cfg.get("retry_backoff_seconds", 30))

        self._notifier: Notifier = notifier
        self._client: TradingClient | None = None

        self._connected: bool = False
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._last_heartbeat: float = 0.0
        self._connect_time: float = 0.0
        self._disconnect_time: float | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def client(self) -> TradingClient:
        """The Alpaca TradingClient instance."""
        if self._client is None:
            raise RuntimeError("Not connected to Alpaca — call connect() first")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_id(self) -> str:
        """Alpaca account number (fetched on connect)."""
        return self._account_id

    @property
    def last_heartbeat(self) -> float:
        return self._last_heartbeat

    @property
    def uptime_seconds(self) -> float:
        if self._connect_time == 0.0:
            return 0.0
        return time.monotonic() - self._connect_time

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Validate Alpaca credentials by fetching the account.  Returns True on success."""
        if not self._api_key or not self._secret_key:
            logger.error(
                "ALPACA_API_KEY and ALPACA_SECRET_KEY must be set as environment variables"
            )
            return False

        try:
            logger.info(
                "Connecting to Alpaca (%s)...",
                "paper" if self._paper else "live",
            )
            self._client = TradingClient(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )
            account: TradeAccount = self._client.get_account()
            self._account_id: str = str(account.account_number)
            self._connected = True
            self._connect_time = time.monotonic()
            self._last_heartbeat = time.monotonic()
            self._disconnect_time = None

            logger.info(
                "Connected to Alpaca (account=%s, equity=$%s, status=%s)",
                self._account_id,
                account.equity,
                account.status,
            )
            return True

        except Exception:
            logger.exception("Failed to connect to Alpaca")
            self._connected = False
            return False

    async def disconnect(self) -> None:
        """Mark as disconnected.  Alpaca REST has no persistent connection to close."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        self._connected = False
        self._client = None
        logger.info("Disconnected from Alpaca")

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    async def start_heartbeat(self) -> None:
        """Launch periodic health check as a background task."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(), name="alpaca-heartbeat"
        )

    async def _heartbeat_loop(self) -> None:
        """Poll the account endpoint every 60 seconds to verify connectivity."""
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    if self._client is None:
                        self._connected = False
                        await self._handle_disconnect()
                        continue

                    self._client.get_account()
                    self._last_heartbeat = time.monotonic()
                    self._connected = True
                    logger.debug("Heartbeat OK")

                except Exception:
                    logger.warning("Heartbeat failed", exc_info=True)
                    self._connected = False
                    await self._handle_disconnect()

        except asyncio.CancelledError:
            return

    async def _handle_disconnect(self) -> None:
        """Attempt to re-establish connectivity."""
        if self._disconnect_time is None:
            self._disconnect_time = time.monotonic()

        await self._notifier.gateway_alert(
            "Alpaca API connection lost. Attempting reconnect...",
            is_critical=False,
        )

        for attempt in range(1, self._max_retries + 1):
            wait: int = self._retry_backoff * (2 ** (attempt - 1))
            logger.info("Reconnect attempt %d/%d in %ds", attempt, self._max_retries, wait)
            await asyncio.sleep(wait)

            if await self.connect():
                downtime: float = 0.0
                if self._disconnect_time is not None:
                    downtime = time.monotonic() - self._disconnect_time
                self._disconnect_time = None
                await self._notifier.gateway_alert(
                    f"Alpaca reconnected after {downtime:.0f}s downtime",
                    is_critical=False,
                )
                return

        await self._notifier.gateway_alert(
            f"Alpaca connection lost — {self._max_retries} attempts failed. "
            "Retrying every 5 min.",
            is_critical=True,
        )
        await self._connection_wait_loop()

    async def _connection_wait_loop(self) -> None:
        """Retry every 5 minutes indefinitely.  The bot never exits."""
        while True:
            await asyncio.sleep(300)
            logger.info("Connection-wait: attempting reconnect")
            if await self.connect():
                downtime: float = 0.0
                if self._disconnect_time is not None:
                    downtime = time.monotonic() - self._disconnect_time
                self._disconnect_time = None
                logger.info("Connection restored after extended outage (%.0fs)", downtime)
                await self._notifier.gateway_alert(
                    f"Alpaca reconnected after extended outage ({downtime:.0f}s downtime)",
                    is_critical=False,
                )
                return

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    async def get_account_summary(self) -> dict[str, Any]:
        """Retrieve account cash, equity, and buying power.

        Returns a dict with keys matching the old IB interface for
        compatibility: ``NetLiquidation``, ``TotalCashValue``,
        ``BuyingPower``, ``SettledCash``.
        """
        if not self.is_connected or self._client is None:
            logger.warning("get_account_summary called while disconnected")
            return {}

        try:
            account: TradeAccount = self._client.get_account()
            return {
                "NetLiquidation": str(account.equity),
                "TotalCashValue": str(account.cash),
                "BuyingPower": str(account.buying_power),
                "SettledCash": str(account.cash),
            }
        except Exception:
            logger.exception("Failed to get account summary")
            return {}

    async def get_positions(self) -> list[AlpacaPosition]:
        """Get all current positions from Alpaca."""
        if not self.is_connected or self._client is None:
            logger.warning("get_positions called while disconnected")
            return []

        try:
            positions: list[AlpacaPosition] = self._client.get_all_positions()
            logger.debug("Alpaca reports %d positions", len(positions))
            return positions
        except Exception:
            logger.exception("Failed to get positions")
            return []

    async def get_open_orders(self) -> list[AlpacaOrder]:
        """Get all pending/active orders from Alpaca."""
        if not self.is_connected or self._client is None:
            logger.warning("get_open_orders called while disconnected")
            return []

        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus
            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders: list[AlpacaOrder] = self._client.get_orders(filter=request)
            logger.debug("Alpaca reports %d open orders", len(orders))
            return orders
        except Exception:
            logger.exception("Failed to get open orders")
            return []

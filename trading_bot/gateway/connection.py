"""Alpaca Trading API connection manager.

Refactored for the tick model (Phase 2c): the long-running heartbeat task and
reconnect alerting logic have been removed because a GHA cron tick is too
short to benefit from them.  ``connect()`` validates credentials by fetching
the account; if it fails the tick aborts.  Public methods keep ``async def``
signatures for caller compat while bodies are synchronous REST calls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from alpaca.trading.client import TradingClient
from alpaca.trading.models import Order as AlpacaOrder
from alpaca.trading.models import Position as AlpacaPosition
from alpaca.trading.models import TradeAccount

from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)


class GatewayConnection:
    """Manages the Alpaca Trading API client for a single tick.

    Alpaca is REST-only — there is no persistent socket to maintain.  The
    ``connect()`` call validates credentials and caches the ``TradingClient``;
    the tick orchestrator calls it once at the start of each run.
    """

    def __init__(self, config: dict[str, Any], notifier: Notifier) -> None:
        alpaca_cfg: dict[str, Any] = config.get("alpaca", {})

        self._api_key: str = os.environ.get("ALPACA_API_KEY", "")
        self._secret_key: str = os.environ.get("ALPACA_SECRET_KEY", "")
        self._paper: bool = bool(alpaca_cfg.get("paper", False))

        self._notifier: Notifier = notifier
        self._client: TradingClient | None = None

        self._connected: bool = False
        self._account_id: str = ""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def client(self) -> TradingClient:
        if self._client is None:
            raise RuntimeError("Not connected to Alpaca — call connect() first")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def account_id(self) -> str:
        return self._account_id

    # ------------------------------------------------------------------
    # Connect / Disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Validate Alpaca credentials by fetching the account."""
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
            self._account_id = str(account.account_number)
            self._connected = True

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
        """Clear the cached client.  No persistent connection to close."""
        self._connected = False
        self._client = None
        logger.debug("Disconnected from Alpaca")

    # ------------------------------------------------------------------
    # Heartbeat — no-op in the tick model.  Kept as a coroutine stub so
    # any remaining callers (main.py) can still ``await`` it without
    # raising.  Remove once main.py is rewritten as a stateless tick.
    # ------------------------------------------------------------------

    async def start_heartbeat(self) -> None:
        logger.debug("start_heartbeat is a no-op in the tick model")

    # ------------------------------------------------------------------
    # Account queries
    # ------------------------------------------------------------------

    async def get_account_summary(self) -> dict[str, Any]:
        """Retrieve account cash, equity, and buying power."""
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

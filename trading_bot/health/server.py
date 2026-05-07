"""Lightweight HTTP health check endpoint for external monitoring.

Exposes a single ``GET /health`` endpoint returning JSON status
of the bot, gateway connection, risk state, and market session info.
Uses ``aiohttp`` to run within the bot's existing async event loop.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiohttp import web

from trading_bot.constants import TZ_EASTERN

if TYPE_CHECKING:
    from trading_bot.config import Config
    from trading_bot.execution.risk_manager import RiskManager
    from trading_bot.gateway.connection import GatewayConnection

logger: logging.Logger = logging.getLogger(__name__)


class HealthServer:
    """Lightweight HTTP server for bot health monitoring.

    Serves a single ``GET /health`` endpoint on the configured host/port.
    The response is a JSON object summarising the current bot state.
    """

    def __init__(
        self,
        config: Config,
        gateway: GatewayConnection,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self._config: Config = config
        self._gateway: GatewayConnection = gateway
        self._risk_manager: RiskManager | None = risk_manager

        self._host: str = config.health_host
        self._port: int = config.health_port

        self._app: web.Application = web.Application()
        self._app.router.add_get("/health", self._handle_health)

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._start_time: float = time.monotonic()

        # External state hooks — callers can update these to reflect
        # bot-level status beyond what gateway/risk expose directly.
        self.bot_status: str = "starting"
        self.stale_symbols: list[str] = []
        self.market_status: dict[str, str] = {"lse": "closed", "us": "closed"}
        self.last_trade_time: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the health check HTTP server."""
        self._start_time = time.monotonic()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self._host, self._port)
        await self._site.start()
        logger.info("Health server started on http://%s:%d/health", self._host, self._port)

    async def stop(self) -> None:
        """Gracefully stop the health check server."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Health server stopped")

    # ------------------------------------------------------------------
    # Request handler
    # ------------------------------------------------------------------

    async def _handle_health(self, request: web.Request) -> web.Response:
        """Handle ``GET /health`` — return JSON bot status.

        Always returns HTTP 200 (the server is reachable). The ``status``
        field in the body distinguishes healthy from degraded states.
        """
        now_et: datetime = datetime.now(TZ_EASTERN)

        # Gateway state
        gateway_connected: bool = self._gateway.is_connected
        uptime_seconds: float = time.monotonic() - self._start_time

        # Status determination
        if not gateway_connected:
            status: str = "degraded"
        elif self.bot_status == "error":
            status = "error"
        elif self.bot_status == "paused":
            status = "paused"
        else:
            status = "running"

        # Account data
        account_equity_usd: float = 0.0
        open_positions_count: int = 0
        daily_pnl_usd: float = 0.0
        trades_today: int = 0
        phase: int = self._config.get_phase().value

        # Pull live data from database
        try:
            db_path: str = self._config.db_path
            conn: sqlite3.Connection = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                # Open positions count
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM positions "
                    "WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED')"
                ).fetchone()
                open_positions_count = int(row["cnt"]) if row else 0

                # Trades today. Uses substr(entry_time, 1, 10) — the
                # first 10 chars of an ET-aware ISO are the ET-local
                # date. SQLite's date() would re-interpret the offset
                # as UTC and silently shift evening trades. See
                # performance.py module docstring.
                today_str: str = now_et.strftime("%Y-%m-%d")
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM trades "
                    "WHERE substr(entry_time, 1, 10) = ?",
                    (today_str,),
                ).fetchone()
                trades_today = int(row["cnt"]) if row else 0

                # Daily P&L
                row = conn.execute(
                    """
                    SELECT COALESCE(SUM(pnl_usd), 0.0) AS total
                    FROM trades
                    WHERE substr(exit_time, 1, 10) = ? AND pnl_usd IS NOT NULL
                    """,
                    (today_str,),
                ).fetchone()
                daily_pnl_usd = float(row["total"]) if row else 0.0

                # Latest equity from daily summaries
                row = conn.execute(
                    "SELECT account_equity_usd FROM daily_summaries ORDER BY date DESC LIMIT 1"
                ).fetchone()
                if row:
                    account_equity_usd = float(row["account_equity_usd"])

            finally:
                conn.close()
        except Exception:
            # Health endpoint must never fail; DB read errors are surfaced elsewhere.
            logger.debug("Health endpoint: could not read database", exc_info=True)

        # Risk status
        risk_status: dict[str, Any] = self._get_risk_status(
            account_equity_usd, daily_pnl_usd, trades_today
        )

        body: dict[str, Any] = {
            "status": status,
            "timestamp": now_et.isoformat(),
            "gateway_connected": gateway_connected,
            "phase": phase,
            "account_equity_usd": round(account_equity_usd, 2),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "open_positions": open_positions_count,
            "trades_today": trades_today,
            "stale_symbols": list(self.stale_symbols),
            "uptime_seconds": round(uptime_seconds, 0),
            "markets": dict(self.market_status),
            "last_trade_time": self.last_trade_time,
            "risk_status": risk_status,
        }

        return web.json_response(body)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_risk_status(
        self,
        account_equity_usd: float,
        daily_pnl_usd: float,
        trades_today: int,
    ) -> dict[str, Any]:
        """Build the risk status sub-object for the health response."""
        daily_loss_limit_pct: float = self._config.daily_loss_limit_pct
        daily_loss_limit_usd: float = account_equity_usd * daily_loss_limit_pct
        daily_loss_remaining: float = daily_loss_limit_usd + daily_pnl_usd  # pnl is negative when losing

        max_daily_trades: int = self._config.get_max_daily_trades()
        trades_remaining: int = max(max_daily_trades - trades_today, 0)

        is_paused: bool = False
        if self._risk_manager is not None:
            # If the risk manager exposes a paused state, use it
            is_paused = getattr(self._risk_manager, "is_paused", False)

        return {
            "daily_loss_remaining_usd": round(max(daily_loss_remaining, 0.0), 2),
            "trades_remaining": trades_remaining,
            "max_daily_trades": max_daily_trades,
            "daily_loss_limit_usd": round(daily_loss_limit_usd, 2),
            "is_paused": bool(is_paused),
        }

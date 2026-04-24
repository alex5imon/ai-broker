"""FX rate management for multi-currency P&L.

Fetches GBP/USD rate from a free API (with fallback to a static rate)
for converting USD positions and P&L to GBP for reporting.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")

_EXCHANGE_RATE_URL: str = "https://open.er-api.com/v6/latest/GBP"


class FXManager:
    """Manages GBP/USD exchange rate via free external API.

    Falls back to a configurable static rate if the API is unavailable.
    """

    def __init__(
        self,
        gateway: Any,
        config: dict[str, Any],
    ) -> None:
        self._config: dict[str, Any] = config

        fx_cfg: dict[str, Any] = config.get("fx", {})
        self._refresh_interval_s: int = int(
            fx_cfg.get("refresh_interval_seconds", 300)
        )
        self._fallback_rate: float = float(
            fx_cfg.get("fallback_gbp_usd", 1.27)
        )

        self._rate: float | None = None
        self._last_update: datetime | None = None
        self._running: bool = False

    # -------------------------------------------------------------------------
    # Rate fetching
    # -------------------------------------------------------------------------

    async def refresh(self) -> None:
        """Fetch current GBP/USD rate from the free exchange rate API."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(_EXCHANGE_RATE_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        logger.warning("FX API returned status %d", resp.status)
                        return
                    data: dict[str, Any] = await resp.json()
                    rates: dict[str, float] = data.get("rates", {})
                    usd_rate: float | None = rates.get("USD")
                    if usd_rate and usd_rate > 0:
                        self._rate = usd_rate
                        self._last_update = datetime.now(tz=ET)
                        logger.debug("GBP/USD rate updated: %.5f", self._rate)
                    else:
                        logger.warning("No USD rate in API response")
        except Exception:
            # Benign: falls back to cached/fallback rate; next tick will retry.
            logger.debug("Failed to fetch GBP/USD rate from API", exc_info=True)

    async def refresh_loop(self) -> None:
        """Background loop refreshing FX rate periodically."""
        self._running = True
        logger.info(
            "FX refresh loop started (interval=%ds, fallback=%.4f)",
            self._refresh_interval_s, self._fallback_rate,
        )

        while self._running:
            try:
                await self.refresh()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Error in FX refresh loop", exc_info=True)

            try:
                await asyncio.sleep(self._refresh_interval_s)
            except asyncio.CancelledError:
                break

        logger.info("FX refresh loop stopped")

    def stop(self) -> None:
        self._running = False

    # -------------------------------------------------------------------------
    # Rate accessors
    # -------------------------------------------------------------------------

    def get_rate(self) -> float:
        """Get current GBP/USD rate (how many USD per 1 GBP)."""
        if self._rate is not None:
            return self._rate
        logger.warning("No live GBP/USD rate, using fallback %.4f", self._fallback_rate)
        return self._fallback_rate

    @property
    def rate(self) -> float:
        return self.get_rate()

    @property
    def last_update(self) -> datetime | None:
        return self._last_update

    @property
    def is_live(self) -> bool:
        return self._rate is not None

    # -------------------------------------------------------------------------
    # Currency conversion
    # -------------------------------------------------------------------------

    def to_gbp(self, amount: float, currency: str) -> float:
        """Convert an amount to GBP."""
        currency_upper: str = currency.upper()

        if currency_upper == "GBP":
            return amount

        if currency_upper == "USD":
            rate: float = self.get_rate()
            if rate <= 0:
                rate = self._fallback_rate
            return amount / rate

        logger.warning("Unknown currency '%s', returning amount unchanged", currency)
        return amount

    def to_usd(self, amount: float, currency: str) -> float:
        """Convert an amount to USD."""
        currency_upper: str = currency.upper()

        if currency_upper == "USD":
            return amount

        if currency_upper == "GBP":
            return amount * self.get_rate()

        logger.warning("Unknown currency '%s', returning amount unchanged", currency)
        return amount

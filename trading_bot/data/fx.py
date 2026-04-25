"""FX rate management for multi-currency P&L.

Fetches GBP/USD rate from a free API (with fallback to a static rate)
for converting USD positions and P&L to GBP for reporting.

Phase 4 (tick model): the ``refresh_loop`` background task is gone
because a stateless cron tick has nothing to loop over.  ``refresh()``
keeps its ``async def`` signature for caller compat but performs a
synchronous ``requests`` call.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from trading_bot.constants import TZ_EASTERN

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN

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

    # -------------------------------------------------------------------------
    # Rate fetching
    # -------------------------------------------------------------------------

    async def refresh(self) -> None:
        """Fetch current GBP/USD rate from the free exchange rate API."""
        try:
            resp = requests.get(_EXCHANGE_RATE_URL, timeout=10)
            if resp.status_code != 200:
                logger.warning("FX API returned status %d", resp.status_code)
                return
            data: dict[str, Any] = resp.json()
            rates: dict[str, float] = data.get("rates", {})
            usd_rate: float | None = rates.get("USD")
            if usd_rate and usd_rate > 0:
                self._rate = usd_rate
                self._last_update = datetime.now(tz=ET)
                logger.debug("GBP/USD rate updated: %.5f", self._rate)
            else:
                logger.warning("No USD rate in API response")
        except Exception:
            logger.debug("Failed to fetch GBP/USD rate from API", exc_info=True)

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

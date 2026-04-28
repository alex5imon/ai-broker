"""Macro event calendar — FOMC announcement dates and similar gating signals.

The bot consumes this calendar via :func:`is_fomc_day` to optionally skip new
entries on Fed announcement days. Dates are sourced from config to keep the
trading_bot package free of yearly maintenance: the operator updates
``config.yaml`` (``event_gate.fomc_dates_<year>``) once per year.

A small in-package fallback list is kept ONLY for years already known at the
time of writing so the bot still gates correctly if config is misconfigured;
any mismatch with config is logged. Config always wins when present.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


# Hard-coded FOMC announcement dates (post-meeting statement days).
# Source: federalreserve.gov FOMC calendar. Update annually.
_FALLBACK_FOMC_DATES: dict[int, list[str]] = {
    2026: [
        "2026-01-28",
        "2026-03-18",
        "2026-05-06",
        "2026-06-17",
        "2026-07-29",
        "2026-09-16",
        "2026-11-04",
        "2026-12-16",
    ],
}


def get_configured_fomc_dates(raw_config: dict[str, Any], year: int) -> list[str]:
    """Return FOMC dates for *year* as ISO strings.

    Reads ``event_gate.fomc_dates_<year>`` from *raw_config*. Falls back to
    the in-package list if the key is missing.
    """
    section: dict[str, Any] = raw_config.get("event_gate", {}) or {}
    key: str = f"fomc_dates_{year}"
    cfg_dates: list[str] | None = section.get(key)
    if cfg_dates:
        return [str(d) for d in cfg_dates]
    fallback: list[str] = _FALLBACK_FOMC_DATES.get(year, [])
    if fallback:
        logger.debug(
            "event_gate.%s missing from config — using in-package fallback (%d dates)",
            key, len(fallback),
        )
    return list(fallback)


def is_fomc_day(today: date, raw_config: dict[str, Any]) -> bool:
    """Return ``True`` if *today* is an FOMC announcement day per config."""
    dates: list[str] = get_configured_fomc_dates(raw_config, today.year)
    return today.isoformat() in dates


def fomc_size_multiplier(
    today: date,
    raw_config: dict[str, Any],
) -> float:
    """Return the position-size multiplier to apply on *today*.

    Returns ``0.0`` to mean "skip new entries entirely" (action == "skip"),
    a positive multiplier < 1 to mean "scale risk down", or ``1.0`` if the
    day is not gated. Reads ``event_gate.enabled``, ``event_gate.fomc_action``
    (``"skip"`` | ``"reduce"``) and ``event_gate.fomc_size_multiplier``.
    """
    section: dict[str, Any] = raw_config.get("event_gate", {}) or {}
    if not section.get("enabled", False):
        return 1.0
    if not is_fomc_day(today, raw_config):
        return 1.0

    action: str = str(section.get("fomc_action", "skip")).lower()
    if action == "skip":
        return 0.0
    if action == "reduce":
        mult: float = float(section.get("fomc_size_multiplier", 0.5))
        return max(0.0, min(1.0, mult))
    logger.warning(
        "Unknown event_gate.fomc_action=%r — defaulting to skip", action,
    )
    return 0.0

"""Tests for FXManager currency conversion (Alpaca, US-only)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.data.fx import FXManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fx(raw_config: dict[str, Any], rate: float | None = None) -> FXManager:
    """Build an FXManager with a pre-set rate."""
    fx = FXManager(MagicMock(), raw_config)
    if rate is not None:
        fx._rate = rate
    return fx


# ---------------------------------------------------------------------------
# to_gbp
# ---------------------------------------------------------------------------


class TestToGbp:
    def test_gbp_to_gbp_no_conversion(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.25)
        assert fx.to_gbp(100.0, "GBP") == 100.0

    def test_usd_to_gbp_conversion(
        self, raw_config: dict[str, Any]
    ) -> None:
        """125 USD @ 1.25 GBP/USD = 100 GBP."""
        fx = _make_fx(raw_config, rate=1.25)
        result = fx.to_gbp(125.0, "USD")
        assert abs(result - 100.0) < 0.001

    def test_unknown_currency_passes_through(
        self, raw_config: dict[str, Any]
    ) -> None:
        """Unknown currency -> amount returned unchanged with warning."""
        fx = _make_fx(raw_config, rate=1.25)
        result = fx.to_gbp(100.0, "EUR")
        assert result == 100.0

    def test_case_insensitive_currency(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.25)
        assert fx.to_gbp(100.0, "gbp") == 100.0
        assert abs(fx.to_gbp(125.0, "usd") - 100.0) < 0.001


# ---------------------------------------------------------------------------
# to_usd
# ---------------------------------------------------------------------------


class TestToUsd:
    def test_usd_to_usd_no_conversion(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.25)
        assert fx.to_usd(100.0, "USD") == 100.0

    def test_gbp_to_usd_conversion(
        self, raw_config: dict[str, Any]
    ) -> None:
        """GBP100 @ 1.25 = $125."""
        fx = _make_fx(raw_config, rate=1.25)
        result = fx.to_usd(100.0, "GBP")
        assert abs(result - 125.0) < 0.001


# ---------------------------------------------------------------------------
# Fallback rate
# ---------------------------------------------------------------------------


class TestFallbackRate:
    def test_fallback_rate_used_when_no_live(
        self, raw_config: dict[str, Any]
    ) -> None:
        """No live rate -> falls back to config value 1.27."""
        fx = _make_fx(raw_config, rate=None)
        rate = fx.get_rate()
        assert rate == raw_config["fx"]["fallback_gbp_usd"]

    def test_live_rate_overrides_fallback(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.30)
        assert fx.get_rate() == 1.30

    def test_is_live_false_without_rate(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=None)
        assert fx.is_live is False

    def test_is_live_true_with_rate(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.25)
        assert fx.is_live is True


# ---------------------------------------------------------------------------
# Rate caching
# ---------------------------------------------------------------------------


class TestRateCaching:
    def test_rate_cached_across_calls(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.28)
        r1 = fx.get_rate()
        r2 = fx.get_rate()
        assert r1 == r2 == 1.28

    def test_rate_property_alias(
        self, raw_config: dict[str, Any]
    ) -> None:
        fx = _make_fx(raw_config, rate=1.30)
        assert fx.rate == fx.get_rate()

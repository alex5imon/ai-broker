"""Unit tests for the time-in-force selection helpers.

Regression coverage for production incident 2026-05-04: every entry signal
hit Alpaca error 42210000 ("fractional orders must be DAY orders") because
stop / emergency-flatten orders were submitted with GTC / IOC TIF.
"""

from __future__ import annotations

import pytest
from alpaca.trading.enums import TimeInForce

from trading_bot.gateway.order_tif import (
    is_fractional,
    tif_for_market,
    tif_for_stop,
)


class TestIsFractional:
    @pytest.mark.parametrize(
        "qty,expected",
        [
            (4.2067, True),
            (0.4412, True),
            (0.001, True),
            (1.5, True),
            (1.0, False),
            (5.0, False),
            (100, False),
            (0, False),
        ],
    )
    def test_detects_fractional(self, qty: float, expected: bool) -> None:
        assert is_fractional(qty) is expected


class TestTifForStop:
    @pytest.mark.parametrize(
        "qty",
        [4.2067, 0.4412, 6.5395, 0.001, 1.5],
    )
    def test_fractional_qty_returns_day(self, qty: float) -> None:
        # Alpaca rejects GTC stops on fractional qty (error 42210000).
        assert tif_for_stop(qty) == TimeInForce.DAY

    @pytest.mark.parametrize(
        "qty",
        [1, 1.0, 5, 100, 1000.0],
    )
    def test_whole_qty_returns_gtc(self, qty: float) -> None:
        # Whole-share stops use GTC so they survive across the day.
        assert tif_for_stop(qty) == TimeInForce.GTC


class TestTifForMarket:
    @pytest.mark.parametrize(
        "qty",
        [4.2067, 0.4412, 6.5395, 0.001, 1.5],
    )
    def test_fractional_qty_returns_day(self, qty: float) -> None:
        # Alpaca rejects IOC market orders on fractional qty (error 42210000).
        assert tif_for_market(qty) == TimeInForce.DAY

    @pytest.mark.parametrize(
        "qty",
        [1, 1.0, 5, 100, 1000.0],
    )
    def test_whole_qty_returns_ioc(self, qty: float) -> None:
        # Whole-share emergency flattens use IOC: fill immediately or kill.
        assert tif_for_market(qty) == TimeInForce.IOC

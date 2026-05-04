"""Time-in-force selection helpers.

Alpaca rejects fractional orders with any time-in-force other than ``DAY``
(error code 42210000: ``fractional orders must be DAY orders``). This
module centralises the selection so every order-submission site uses the
same rule and a regression in any single call site can't slip through.
"""

from __future__ import annotations

from alpaca.trading.enums import TimeInForce


def is_fractional(qty: float) -> bool:
    """Return True if ``qty`` has a non-zero fractional component."""
    qty_f: float = float(qty)
    return qty_f != int(qty_f)


def tif_for_stop(qty: float) -> TimeInForce:
    """TIF for stop / trailing-stop orders.

    Whole-share stops use ``GTC`` so they survive across the day. Fractional
    must be ``DAY`` per Alpaca. The stateless tick + recovery loop will
    re-attach a missing stop on the next trading day's first tick.
    """
    return TimeInForce.DAY if is_fractional(qty) else TimeInForce.GTC


def tif_for_market(qty: float) -> TimeInForce:
    """TIF for emergency-flatten / market exit orders.

    Whole-share markets use ``IOC`` (fill immediately or kill). Fractional
    must be ``DAY``: during market hours a market order with DAY TIF still
    fills immediately if there is liquidity, so the semantic gap only
    matters off-hours — and emergency_flatten is gated to trading hours.
    """
    return TimeInForce.DAY if is_fractional(qty) else TimeInForce.IOC

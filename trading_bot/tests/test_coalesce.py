"""Tests for trading_bot.utils.coalesce."""

from trading_bot.utils import coalesce


def test_missing_key_returns_default() -> None:
    assert coalesce({}, "x", 42) == 42


def test_present_none_returns_default() -> None:
    # This is the trap that motivated coalesce: SQLite NULL -> Python None.
    # dict.get("x", 42) would return None here, not 42.
    assert coalesce({"x": None}, "x", 42) == 42


def test_present_non_none_returns_value() -> None:
    assert coalesce({"x": 7}, "x", 42) == 7
    assert coalesce({"x": 0}, "x", 42) == 0       # falsy but not None
    assert coalesce({"x": ""}, "x", "fallback") == ""
    assert coalesce({"x": False}, "x", True) is False

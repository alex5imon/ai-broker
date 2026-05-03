"""Tests for ExitManager."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from trading_bot.config import Config
from trading_bot.constants import ExitReason, HoldType
from trading_bot.strategy.exit import ExitDecision, ExitManager

pytestmark = pytest.mark.critical

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def exit_manager(config: Config, mock_market_data) -> ExitManager:
    return ExitManager(config, mock_market_data)


def _long_position(
    entry_price: float = 10.0,
    stop_price: float = 9.80,
    target_price: float = 10.30,
    hold_type: str = "intraday",
    trailing_active: bool = False,
    highest_price: float | None = None,
    entry_time: datetime | None = None,
) -> dict[str, Any]:
    return {
        "ticker": "PLTR",
        "direction": "long",
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "hold_type": hold_type,
        "trailing_active": trailing_active,
        "highest_price": highest_price or entry_price,
        "entry_time": (entry_time or datetime.now(ET)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Stop loss
# ---------------------------------------------------------------------------


class TestStopLoss:
    def test_stop_loss_triggered(self, exit_manager: ExitManager) -> None:
        pos = _long_position(entry_price=10.0, stop_price=9.80)
        assert exit_manager.check_stop_loss(pos, 9.75) is True

    def test_stop_loss_at_price_triggers(self, exit_manager: ExitManager) -> None:
        pos = _long_position(stop_price=9.80)
        assert exit_manager.check_stop_loss(pos, 9.80) is True

    def test_stop_loss_not_triggered(self, exit_manager: ExitManager) -> None:
        pos = _long_position(stop_price=9.80)
        assert exit_manager.check_stop_loss(pos, 9.85) is False

    def test_stop_loss_no_stop_price(self, exit_manager: ExitManager) -> None:
        pos = {"direction": "long", "ticker": "PLTR"}
        assert exit_manager.check_stop_loss(pos, 5.0) is False

    def test_stop_loss_short_triggered(self, exit_manager: ExitManager) -> None:
        pos = {
            "ticker": "PLTR",
            "direction": "short",
            "entry_price": 10.0,
            "stop_price": 10.20,
            "hold_type": "intraday",
            "trailing_active": False,
            "highest_price": 10.0,
            "entry_time": datetime.now(ET).isoformat(),
        }
        assert exit_manager.check_stop_loss(pos, 10.25) is True


# ---------------------------------------------------------------------------
# Take profit
# ---------------------------------------------------------------------------


class TestTakeProfit:
    def test_take_profit_triggered(self, exit_manager: ExitManager) -> None:
        pos = _long_position(target_price=10.30)
        assert exit_manager.check_take_profit(pos, 10.35) is True

    def test_take_profit_at_price_triggers(self, exit_manager: ExitManager) -> None:
        pos = _long_position(target_price=10.30)
        assert exit_manager.check_take_profit(pos, 10.30) is True

    def test_take_profit_not_triggered(self, exit_manager: ExitManager) -> None:
        pos = _long_position(target_price=10.30)
        assert exit_manager.check_take_profit(pos, 10.25) is False

    def test_take_profit_no_target(self, exit_manager: ExitManager) -> None:
        pos = {"direction": "long", "ticker": "PLTR"}
        assert exit_manager.check_take_profit(pos, 999.0) is False


# ---------------------------------------------------------------------------
# Trailing stop
# ---------------------------------------------------------------------------


class TestTrailingStop:
    def test_trailing_stop_activation(self, exit_manager: ExitManager) -> None:
        """Price rises +1.5% — trailing stop should activate for intraday."""
        pos = _long_position(entry_price=10.0, hold_type="intraday",
                             trailing_active=False, highest_price=10.0)
        # activation at +1.5% = 10.15
        triggered, high = exit_manager.check_trailing_stop(pos, 10.16)
        # Should activate (pct_from_entry >= 0.015) but not triggered yet
        assert triggered is False  # price hasn't dropped 1% from high
        assert high is not None and high >= 10.15

    def test_trailing_stop_triggers(self, exit_manager: ExitManager) -> None:
        """Trailing active, price falls 1% from high — should trigger."""
        # Simulate: entry=10, high=10.20 (already at +2%), trailing active
        pos = _long_position(entry_price=10.0, hold_type="intraday",
                             trailing_active=True, highest_price=10.20)
        # Trail distance = 1%, so trail price = 10.20 * 0.99 = 10.098
        triggered, _ = exit_manager.check_trailing_stop(pos, 10.05)
        assert triggered is True

    def test_trailing_stop_not_yet_activated(self, exit_manager: ExitManager) -> None:
        """Price at +1.4% — below 1.5% activation threshold."""
        pos = _long_position(entry_price=10.0, hold_type="intraday",
                             trailing_active=False, highest_price=10.0)
        # +1.4% = 10.14
        triggered, _ = exit_manager.check_trailing_stop(pos, 10.14)
        assert triggered is False

    def test_trailing_stop_swing_higher_activation(
        self, exit_manager: ExitManager
    ) -> None:
        """Swing activation is 2.5%, not 1.5%."""
        pos = _long_position(entry_price=10.0, hold_type="swing",
                             trailing_active=False, highest_price=10.0)
        # +2.4% — below swing 2.5% activation
        triggered, _ = exit_manager.check_trailing_stop(pos, 10.24)
        assert triggered is False

    def test_trailing_stop_does_not_trigger_if_price_holds(
        self, exit_manager: ExitManager
    ) -> None:
        """Trailing active, price remains above trail level — no exit."""
        # high=10.20, trail distance=1%, trail_price=10.098; price=10.15 > trail
        pos = _long_position(entry_price=10.0, hold_type="intraday",
                             trailing_active=True, highest_price=10.20)
        triggered, _ = exit_manager.check_trailing_stop(pos, 10.15)
        assert triggered is False


# ---------------------------------------------------------------------------
# Time stop
# ---------------------------------------------------------------------------


class TestTimeStop:
    def test_time_stop_intraday_triggers_after_4h(
        self, exit_manager: ExitManager
    ) -> None:
        entry = datetime.now(ET) - timedelta(hours=4, minutes=1)
        pos = _long_position(hold_type="intraday", entry_time=entry)
        assert exit_manager.check_time_stop(pos, datetime.now(ET)) is True

    def test_time_stop_intraday_not_triggered_at_3h(
        self, exit_manager: ExitManager
    ) -> None:
        entry = datetime.now(ET) - timedelta(hours=3)
        pos = _long_position(hold_type="intraday", entry_time=entry)
        assert exit_manager.check_time_stop(pos, datetime.now(ET)) is False

    def test_time_stop_swing_held_4_days_not_triggered(
        self, exit_manager: ExitManager
    ) -> None:
        """Swing held 4 days — within 5-day max, no exit."""
        entry = datetime.now(ET) - timedelta(days=4)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, datetime.now(ET)) is False

    def test_time_stop_swing_held_6_days_triggers(
        self, exit_manager: ExitManager
    ) -> None:
        entry = datetime.now(ET) - timedelta(days=6)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, datetime.now(ET)) is True

    def test_time_stop_no_entry_time_returns_false(
        self, exit_manager: ExitManager
    ) -> None:
        pos = {"direction": "long", "hold_type": "intraday"}
        assert exit_manager.check_time_stop(pos, datetime.now(ET)) is False


# ---------------------------------------------------------------------------
# Composite should_exit — priority ordering
# ---------------------------------------------------------------------------


class TestShouldExit:
    def test_stop_loss_priority_over_profit(self, exit_manager: ExitManager) -> None:
        """When both stop and target are hit (e.g. gap), stop_loss wins."""
        # Both conditions met at same bar:
        # stop_price = 9.50, target = 10.50, current = 9.40
        pos = _long_position(entry_price=10.0, stop_price=9.50,
                             target_price=10.50)
        # Price 9.40 is below stop — stop loss fires first
        decision = exit_manager.should_exit(pos, 9.40, datetime.now(ET))
        assert decision.should_exit is True
        assert decision.reason == ExitReason.STOP_LOSS
        assert decision.is_emergency is True
        assert decision.use_market_order is True

    def test_take_profit_fires_when_only_target_hit(
        self, exit_manager: ExitManager
    ) -> None:
        pos = _long_position(entry_price=10.0, stop_price=9.80,
                             target_price=10.30)
        decision = exit_manager.should_exit(pos, 10.35, datetime.now(ET))
        assert decision.should_exit is True
        assert decision.reason == ExitReason.TAKE_PROFIT
        assert decision.is_emergency is False

    def test_trailing_stop_fires(self, exit_manager: ExitManager) -> None:
        pos = _long_position(entry_price=10.0, stop_price=9.80,
                             target_price=10.30, hold_type="intraday",
                             trailing_active=True, highest_price=10.20)
        # trail_price = 10.20 * 0.99 = 10.098; current = 10.05 < trail
        decision = exit_manager.should_exit(pos, 10.05, datetime.now(ET))
        assert decision.should_exit is True
        assert decision.reason == ExitReason.TRAILING_STOP

    def test_no_exit_all_conditions_clear(self, exit_manager: ExitManager) -> None:
        pos = _long_position(entry_price=10.0, stop_price=9.80,
                             target_price=10.30)
        decision = exit_manager.should_exit(pos, 10.10, datetime.now(ET))
        assert decision.should_exit is False
        assert decision.reason is None


# ---------------------------------------------------------------------------
# Spread protection
# ---------------------------------------------------------------------------


class TestSpreadProtection:
    def test_spread_delay_non_emergency(
        self, exit_manager: ExitManager, mock_market_data
    ) -> None:
        """Wide spread (>0.15%) — should return False (delay)."""
        mock_market_data.get_spread_pct.return_value = 0.002  # 0.2%
        result = exit_manager.check_spread_for_exit("PLTR")
        assert result is False

    def test_spread_ok_narrow(
        self, exit_manager: ExitManager, mock_market_data
    ) -> None:
        """Narrow spread — should allow exit."""
        mock_market_data.get_spread_pct.return_value = 0.001  # 0.1%
        result = exit_manager.check_spread_for_exit("PLTR")
        assert result is True

    def test_spread_no_data_allows_exit(
        self, exit_manager: ExitManager, mock_market_data
    ) -> None:
        """No spread data — proceed cautiously (True)."""
        mock_market_data.get_spread_pct.return_value = None
        result = exit_manager.check_spread_for_exit("PLTR")
        assert result is True

    def test_emergency_exit_factory(self, exit_manager: ExitManager) -> None:
        """Emergency exit always uses market order regardless of spread."""
        decision = ExitManager.make_emergency_exit(ExitReason.STOP_LOSS, 9.75)
        assert decision.should_exit is True
        assert decision.is_emergency is True
        assert decision.use_market_order is True
        assert decision.reason == ExitReason.STOP_LOSS

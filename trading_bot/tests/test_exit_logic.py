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
# Trailing stop is broker-managed
# ---------------------------------------------------------------------------


class TestTrailingStopIsBrokerManaged:
    """The software-side trailing stop was removed in favour of Alpaca's
    native ``TrailingStopOrderRequest``. ``ExitManager`` no longer offers
    a ``check_trailing_stop`` method, and ``should_exit`` never produces
    an ``ExitReason.TRAILING_STOP`` decision — the broker fires the stop
    autonomously and the bot picks up the fill via order-status polling.
    See ``risk_infrastructure_gaps.md`` item 2.
    """

    def test_check_trailing_stop_is_removed(
        self, exit_manager: ExitManager
    ) -> None:
        assert not hasattr(exit_manager, "check_trailing_stop")

    def test_should_exit_does_not_fire_trailing_stop(
        self, exit_manager: ExitManager
    ) -> None:
        # Position deeply in trailing-stop territory: high=10.20, trailing
        # active, current price 10.05 (would have triggered the old
        # software-side trail). Stop loss / take profit / time stop are
        # all clear.
        pos = _long_position(
            entry_price=10.0, stop_price=9.80, target_price=10.30,
            hold_type="intraday", trailing_active=True,
            highest_price=10.20,
        )
        decision = exit_manager.should_exit(pos, 10.05, datetime.now(ET))
        assert decision.should_exit is False
        assert decision.reason is None


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

    def test_time_stop_swing_held_4_trading_days_not_triggered(
        self, exit_manager: ExitManager
    ) -> None:
        """Swing held 4 trading days (Mon → Fri) — under the 5-day max."""
        # 2026-05-04 is a Monday; 2026-05-08 is the following Friday.
        # Mon → Fri spans Tue, Wed, Thu, Fri = 4 trading days (no holidays).
        entry = datetime(2026, 5, 4, 10, 0, tzinfo=ET)
        now = datetime(2026, 5, 8, 16, 0, tzinfo=ET)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, now) is False

    def test_time_stop_swing_held_5_trading_days_triggers(
        self, exit_manager: ExitManager
    ) -> None:
        """Swing held exactly 5 trading days (Mon → following Mon)."""
        # Mon 2026-05-04 → Mon 2026-05-11 spans Tue, Wed, Thu, Fri, Mon
        # = 5 trading days, which hits the 5-day max.
        entry = datetime(2026, 5, 4, 10, 0, tzinfo=ET)
        now = datetime(2026, 5, 11, 10, 0, tzinfo=ET)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, now) is True

    def test_time_stop_swing_thursday_to_tuesday_not_triggered(
        self, exit_manager: ExitManager
    ) -> None:
        """Regression: Thu → Tue is 5 calendar days but only 3 trading
        days (Fri, Mon, Tue). Under the old calendar-day logic this
        triggered the stop on day 5; under the new trading-day logic it
        must not (the position has only had 3 sessions to work).
        """
        # Thu 2026-05-07 → Tue 2026-05-12 = 5 calendar days, 3 trading days.
        entry = datetime(2026, 5, 7, 10, 0, tzinfo=ET)
        now = datetime(2026, 5, 12, 16, 0, tzinfo=ET)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, now) is False

    def test_time_stop_swing_skips_holidays(
        self, exit_manager: ExitManager
    ) -> None:
        """Regression: Memorial Day (Mon 2026-05-25) does NOT count as a
        trading day. Tue 2026-05-19 → Wed 2026-05-27 = 8 calendar days,
        but only 5 trading days (Wed, Thu, Fri, Tue, Wed) because
        weekends + Memorial Day are excluded. This is the trigger boundary.
        """
        entry = datetime(2026, 5, 19, 10, 0, tzinfo=ET)
        now = datetime(2026, 5, 27, 16, 0, tzinfo=ET)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, now) is True

    def test_time_stop_swing_holiday_keeps_position_alive(
        self, exit_manager: ExitManager
    ) -> None:
        """One trading day short due to Memorial Day: must NOT trigger."""
        # Wed 2026-05-20 → Wed 2026-05-27 = 7 calendar days, but Memorial
        # Day Mon 2026-05-25 is excluded → 4 trading days (Thu, Fri, Tue,
        # Wed). Under calendar-day logic this would have triggered at 7
        # days; under trading-day logic the position keeps running.
        entry = datetime(2026, 5, 20, 10, 0, tzinfo=ET)
        now = datetime(2026, 5, 27, 16, 0, tzinfo=ET)
        pos = _long_position(hold_type="swing", entry_time=entry)
        assert exit_manager.check_time_stop(pos, now) is False

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

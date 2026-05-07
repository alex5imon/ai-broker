"""Tests for RiskManager."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from trading_bot.config import Config
from trading_bot.constants import GICS_SECTOR
from trading_bot.execution.risk_manager import RiskManager

pytestmark = pytest.mark.critical


def _noop_ensure_future(coro: object) -> None:
    """Swallow fire-and-forget coroutines without needing an event loop."""
    if hasattr(coro, "close"):
        coro.close()  # type: ignore[union-attr]

ET = ZoneInfo("US/Eastern")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def risk_manager(config: Config, tmp_db_path: str, mock_notifier) -> RiskManager:
    rm = RiskManager(config, tmp_db_path, mock_notifier)
    return rm


# ---------------------------------------------------------------------------
# Daily loss limit
# ---------------------------------------------------------------------------


class TestDailyLossLimit:
    def test_daily_loss_limit_blocks_trading(
        self, risk_manager: RiskManager
    ) -> None:
        """Loss = -1.1% of equity → limit breached → can_trade() = False."""
        # equity $1000, limit = 1% = $10; current P&L = -$11
        risk_manager.check_daily_loss_limit(-11.0, 1000.0)
        ok, reason = risk_manager.can_trade()
        assert ok is False
        assert reason is not None

    def test_daily_loss_limit_not_hit(self, risk_manager: RiskManager) -> None:
        """Loss = -0.5% of equity → below limit → can_trade() = True."""
        risk_manager.check_daily_loss_limit(-5.0, 1000.0)
        ok, reason = risk_manager.can_trade()
        assert ok is True
        assert reason is None

    def test_daily_loss_limit_exactly_at_threshold(
        self, risk_manager: RiskManager
    ) -> None:
        """Exactly -1% — check that <= triggers it."""
        risk_manager.check_daily_loss_limit(-10.0, 1000.0)
        ok, _ = risk_manager.can_trade()
        assert ok is False

    def test_daily_loss_returns_true_when_breached(
        self, risk_manager: RiskManager
    ) -> None:
        result = risk_manager.check_daily_loss_limit(-15.0, 1000.0)
        assert result is True

    def test_daily_loss_returns_false_when_not_breached(
        self, risk_manager: RiskManager
    ) -> None:
        result = risk_manager.check_daily_loss_limit(-5.0, 1000.0)
        assert result is False


# ---------------------------------------------------------------------------
# Max positions
# ---------------------------------------------------------------------------


class TestMaxPositions:
    def test_max_positions_blocks_entry_phase1(
        self, risk_manager: RiskManager
    ) -> None:
        """Phase 1 max = 2; 2 open positions → blocks."""
        assert risk_manager.check_max_positions(2) is True

    def test_max_positions_allows_below_limit(
        self, risk_manager: RiskManager
    ) -> None:
        """1 open position with Phase 1 max=2 → allowed."""
        assert risk_manager.check_max_positions(1) is False

    def test_max_positions_zero_open_allowed(
        self, risk_manager: RiskManager
    ) -> None:
        assert risk_manager.check_max_positions(0) is False


# ---------------------------------------------------------------------------
# Sector exposure
# ---------------------------------------------------------------------------


class TestSectorExposure:
    def _fin_position(self, ticker: str = "SOFI") -> dict:
        return {"ticker": ticker, "sector": GICS_SECTOR.get(ticker, "Financials")}

    def test_sector_exposure_blocks_second_financials_phase1(
        self, risk_manager: RiskManager
    ) -> None:
        """Phase 1 max_sector=1; already one Financials → second blocked."""
        positions = [self._fin_position("SOFI")]
        assert risk_manager.check_sector_exposure("Financials", positions) is True

    def test_sector_exposure_allows_first_in_sector(
        self, risk_manager: RiskManager
    ) -> None:
        assert risk_manager.check_sector_exposure("Financials", []) is False

    def test_different_sectors_both_allowed(
        self, risk_manager: RiskManager
    ) -> None:
        """Financials + Energy — second sector should still be allowed."""
        positions = [{"ticker": "SOFI", "sector": "Financials"}]
        assert risk_manager.check_sector_exposure("Energy", positions) is False

    def test_sector_from_gics_map_when_no_sector_key(
        self, risk_manager: RiskManager
    ) -> None:
        """Position dict with only ticker — sector resolved via GICS_SECTOR."""
        positions = [{"ticker": "BAC"}]  # no sector key
        assert risk_manager.check_sector_exposure("Financials", positions) is True


# ---------------------------------------------------------------------------
# Drawdown breaker
# ---------------------------------------------------------------------------


class TestDrawdownBreaker:
    def test_drawdown_breaker_triggers(
        self, risk_manager: RiskManager, tmp_db_path: str
    ) -> None:
        """Equity drops 5% from 5-day peak → breaker fires → trading paused."""
        import sqlite3

        # Insert 5 days of equity history at $1000 (peak)
        conn = sqlite3.connect(tmp_db_path)
        from datetime import date, timedelta
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i + 1)).isoformat()
            conn.execute(
                """INSERT OR REPLACE INTO daily_summaries
                   (date, account_equity_usd, phase)
                   VALUES (?, ?, 1)""",
                (d, 1000.0),
            )
        conn.commit()
        conn.close()

        # Current equity = 940 (6% drop from 1000 peak)
        with patch("asyncio.ensure_future", _noop_ensure_future):
            triggered = risk_manager.check_drawdown_breaker(940.0)
        assert triggered is True
        assert risk_manager.is_paused is True

    def test_drawdown_breaker_not_triggered_small_drop(
        self, risk_manager: RiskManager, tmp_db_path: str
    ) -> None:
        """3% drop — below 5% threshold → no trigger."""
        import sqlite3
        from datetime import date, timedelta

        conn = sqlite3.connect(tmp_db_path)
        today = date.today()
        for i in range(5):
            d = (today - timedelta(days=i + 1)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO daily_summaries (date, account_equity_usd, phase) VALUES (?,?,1)",
                (d, 1000.0),
            )
        conn.commit()
        conn.close()

        triggered = risk_manager.check_drawdown_breaker(975.0)
        assert triggered is False

    def test_drawdown_recovery_size_pct(
        self, risk_manager: RiskManager
    ) -> None:
        """After breaker, recovery_size_pct should be 0.5."""
        # Manually activate the breaker
        risk_manager._drawdown_breaker_active = True
        risk_manager._recovery_trades_remaining = 3
        risk_manager._recovery_size_pct = 0.50
        assert risk_manager.recovery_size_pct == 0.50

    def test_recovery_size_pct_normal_when_no_breaker(
        self, risk_manager: RiskManager
    ) -> None:
        assert risk_manager.recovery_size_pct == 1.0


# ---------------------------------------------------------------------------
# Order rejection pause
# ---------------------------------------------------------------------------


class TestOrderRejections:
    def test_order_rejection_pause_after_three(
        self, risk_manager: RiskManager
    ) -> None:
        """4 rejections in 10 min → trading paused."""
        for i in range(4):
            risk_manager.record_rejection("PLTR", f"reason_{i}")
        with patch("asyncio.ensure_future", _noop_ensure_future):
            paused = risk_manager.check_order_rejections()
        assert paused is True

    def test_order_rejection_no_pause_with_two(
        self, risk_manager: RiskManager
    ) -> None:
        """Only 2 rejections — below threshold of 3."""
        risk_manager.record_rejection("PLTR", "r1")
        risk_manager.record_rejection("PLTR", "r2")
        paused = risk_manager.check_order_rejections()
        assert paused is False

    def test_order_rejection_pauses_at_threshold_exactly(
        self, risk_manager: RiskManager
    ) -> None:
        """Regression: pre-fix used `len > max_count` (=4), so 3
        rejections did not trip despite `max_count=3` config naming.
        The rule is now `len >= max_count`."""
        for i in range(3):
            risk_manager.record_rejection("PLTR", f"r{i}")
        with patch("asyncio.ensure_future", _noop_ensure_future):
            paused = risk_manager.check_order_rejections()
        assert paused is True

    def test_rejection_count_increments(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.record_rejection("PLTR", "test")
        assert len(risk_manager._recent_rejections) == 1


# ---------------------------------------------------------------------------
# Commission budget
# ---------------------------------------------------------------------------


class TestCommissionBudget:
    def test_commission_budget_warning(
        self, risk_manager: RiskManager
    ) -> None:
        """Commissions = 25% of gross P&L → warning."""
        result = risk_manager.check_commission_budget(
            daily_commissions=25.0,
            daily_gross_pnl=100.0,
        )
        assert result == "warning"

    def test_commission_budget_stop(
        self, risk_manager: RiskManager
    ) -> None:
        """Commissions = 55% of gross P&L → stop."""
        result = risk_manager.check_commission_budget(
            daily_commissions=55.0,
            daily_gross_pnl=100.0,
        )
        assert result == "stop"

    def test_commission_budget_ok(self, risk_manager: RiskManager) -> None:
        """Commissions = 10% of gross P&L → no action."""
        result = risk_manager.check_commission_budget(
            daily_commissions=10.0,
            daily_gross_pnl=100.0,
        )
        assert result is None

    def test_commission_stop_blocks_can_trade(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.check_commission_budget(55.0, 100.0)
        ok, reason = risk_manager.can_trade()
        assert ok is False
        assert reason is not None


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


class TestDailyReset:
    def test_reset_daily_clears_counters(
        self, risk_manager: RiskManager
    ) -> None:
        # Dirty state
        risk_manager.record_trade(-10.0, 1.0)
        risk_manager.check_daily_loss_limit(-11.0, 1000.0)
        risk_manager.record_rejection("PLTR", "x")

        risk_manager.reset_daily()

        assert risk_manager.daily_pnl_usd == 0.0
        assert risk_manager.trade_count == 0
        assert len(risk_manager._recent_rejections) == 0
        ok, _ = risk_manager.can_trade()
        assert ok is True

    def test_reset_clears_commission_stop(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.check_commission_budget(60.0, 100.0)
        risk_manager.reset_daily()
        ok, reason = risk_manager.can_trade()
        assert ok is True

    def test_record_trade_increments_counters(
        self, risk_manager: RiskManager
    ) -> None:
        risk_manager.record_trade(5.0, 1.0)
        risk_manager.record_trade(-3.0, 0.5)
        assert risk_manager.trade_count == 2
        assert abs(risk_manager.daily_pnl_usd - 2.0) < 0.01

    def test_record_trade_gross_pnl_excludes_losses(
        self, risk_manager: RiskManager
    ) -> None:
        """Regression: ``_daily_gross_pnl_usd`` is the denominator in
        ``commissions / gross_pnl``. Pre-fix it was ``abs(pnl) + comm``,
        which counted losses (and double-counted commissions), inflating
        the denominator and making the commission stop fire late or
        never. Should be the sum of WINNING trade gross profits only."""
        risk_manager.record_trade(10.0, 0.0)   # winner +10
        risk_manager.record_trade(-5.0, 0.0)   # loser -5 — must NOT contribute
        risk_manager.record_trade(3.0, 0.0)    # winner +3
        # Expected gross profit = 10 + 3 = 13 (losses excluded)
        assert abs(risk_manager._daily_gross_pnl_usd - 13.0) < 0.01


# ---------------------------------------------------------------------------
# Cross-tick state persistence (item 1 — risk_circuit_state)
# ---------------------------------------------------------------------------


class TestStatePersistence:
    """RiskManager state must survive across the stateless cron tick.

    Pre-fix: every tick constructed a fresh RiskManager with all defaults,
    silently inerting the daily-loss-limit, drawdown-breaker, pause, and
    commission-stop circuits. See risk_infrastructure_gaps.md item 1.
    """

    def test_first_session_writes_sentinel_row(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        """First session against a fresh DB must stamp the
        ``risk_manager:global`` row even when no breaker has tripped.

        Without this the row is "write-only-on-fault" — external
        monitoring can't tell PR #79's persistence path is wired
        until something actually goes wrong. Discovered 2026-05-07
        evening: PR #79 shipped mid-day, end-of-day DB had no row.
        """
        import sqlite3

        from trading_bot.db.repository import load_risk_state

        # Fresh RiskManager against a brand-new (no risk_manager:global
        # row yet) DB.
        RiskManager(config, tmp_db_path, mock_notifier)

        with sqlite3.connect(tmp_db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = load_risk_state(conn, "risk_manager:global")

        assert row is not None, (
            "first-session bootstrap must write the sentinel row so "
            "the persistence path is verifiable before any breaker fires"
        )
        # Default state — nothing tripped.
        assert row["tripped"] is False

    def test_pause_survives_new_instance(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1.pause_trading("test pause")

        # Simulate new tick — fresh process, fresh RiskManager.
        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        ok, reason = rm2.can_trade()
        assert ok is False
        assert reason == "test pause"

    def test_drawdown_breaker_survives_new_instance(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1._activate_drawdown_breaker(0.06)
        assert rm1.drawdown_breaker_active is True

        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        assert rm2.drawdown_breaker_active is True

    def test_recovery_trades_remaining_survives_new_instance(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1._activate_drawdown_breaker(0.06)
        # Mid-recovery — one winning trade has decremented the counter.
        rm1.record_trade(5.0, 0.0)

        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        # Recovery state, including the decremented counter, persists.
        assert rm2.drawdown_breaker_active is True
        assert rm2._recovery_trades_remaining == rm1._recovery_trades_remaining

    def test_daily_loss_limit_hit_survives_within_same_day(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1.check_daily_loss_limit(-100.0, 1000.0)  # -10% — well past 1% limit
        ok1, _ = rm1.can_trade()
        assert ok1 is False

        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        ok2, _ = rm2.can_trade()
        assert ok2 is False, (
            "daily-loss-limit hit must persist across ticks within the same day"
        )

    def test_commission_stop_survives_new_instance(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        # >50% commission ratio — fires commission stop.
        rm1._trade_count = 5
        rm1.check_commission_budget(daily_commissions=10.0, daily_gross_pnl=15.0)
        assert rm1._commission_stop_active is True

        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        assert rm2._commission_stop_active is True
        ok, _ = rm2.can_trade()
        assert ok is False

    def test_resume_clears_persisted_pause(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1.pause_trading("temporary")

        rm1.resume_trading()

        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        ok, _ = rm2.can_trade()
        assert ok is True
        assert rm2.is_paused is False

    def test_day_rollover_zeros_day_scoped_only(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        """A new day must reset trade_count/daily_pnl/daily_loss_limit_hit
        but preserve the pause/drawdown breaker if they're still active."""
        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        # Set up state: pause active, daily-loss-limit hit, some trade count.
        rm1.pause_trading("multi-day pause")
        # Set pause_until to tomorrow so it survives day rollover.
        rm1._pause_until = datetime.now(tz=ET) + timedelta(days=2)
        rm1.check_daily_loss_limit(-100.0, 1000.0)
        rm1._trade_count = 7
        # Force-persist with the updated pause_until
        rm1._persist_state()

        # Construct a new instance "tomorrow".
        from trading_bot.execution import risk_manager as rm_module
        future = datetime.now(tz=ET) + timedelta(days=1)
        with patch.object(
            rm_module, "datetime",
            new=type("D", (), {
                "now": staticmethod(lambda tz=None: future),
                "fromisoformat": datetime.fromisoformat,
            }),
        ):
            rm2 = RiskManager(config, tmp_db_path, mock_notifier)

        # Day-scoped fields zeroed:
        assert rm2._daily_loss_limit_hit is False
        assert rm2._trade_count == 0
        # Cross-day fields preserved:
        assert rm2.is_paused is True
        assert rm2._pause_reason == "multi-day pause"

    def test_record_rejection_persists_via_order_rejections_table(
        self, config: Config, tmp_db_path: str, mock_notifier
    ) -> None:
        """The deque uses time.monotonic() which is meaningless across
        processes. New instance should rebuild the deque from the
        order_rejections table."""
        import sqlite3

        rm1 = RiskManager(config, tmp_db_path, mock_notifier)
        rm1.record_rejection("SPY", "out_of_money")
        rm1.record_rejection("QQQ", "out_of_money")

        # Sanity: rejections actually landed in the DB.
        conn = sqlite3.connect(tmp_db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM order_rejections"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 2

        # New instance hydrates the deque from the table within window.
        rm2 = RiskManager(config, tmp_db_path, mock_notifier)
        assert len(rm2._recent_rejections) == 2

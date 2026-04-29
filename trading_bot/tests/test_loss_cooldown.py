"""Tests for the per-strategy consecutive-loss cooldown tracker."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

from trading_bot.constants import TZ_EASTERN
from trading_bot.db import repository as repo
from trading_bot.execution.loss_cooldown import (
    LossCooldownConfig,
    LossCooldownTracker,
)

ET: ZoneInfo = TZ_EASTERN

# Fixed clock used across the suite — pin to a well-known mid-day so any
# arithmetic on the cooldown window remains comprehensible.
_T0: datetime = datetime(2026, 4, 28, 14, 30, tzinfo=ET)


def _make_tracker(
    db_path: str,
    now_fn: Callable[[], datetime] | None = None,
    **overrides,
) -> LossCooldownTracker:
    cfg = LossCooldownConfig(
        enabled=overrides.get("enabled", True),
        threshold_losses=overrides.get("threshold_losses", 3),
        cooldown_minutes=overrides.get("cooldown_minutes", 60),
    )
    return LossCooldownTracker(
        db_path=db_path, config=cfg, now_fn=now_fn,
    )


class TestLossCooldownTracker:
    def test_disabled_short_circuits(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, enabled=False)
        for _ in range(10):
            tracker.record_outcome("mean_reversion", -10.0)
        active, reason = tracker.is_on_cooldown("mean_reversion")
        assert active is False
        assert reason is None

    def test_streak_under_threshold_does_not_pause(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, threshold_losses=3)
        tracker.record_outcome("mean_reversion", -5.0)
        tracker.record_outcome("mean_reversion", -5.0)
        active, _ = tracker.is_on_cooldown("mean_reversion")
        assert active is False

    def test_threshold_engages_cooldown(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(
            tmp_db_path, now_fn=lambda: _T0,
            threshold_losses=3, cooldown_minutes=30,
        )
        for _ in range(3):
            tracker.record_outcome("mean_reversion", -5.0)
        active, reason = tracker.is_on_cooldown("mean_reversion")
        assert active is True
        assert reason is not None
        assert "consecutive losses" in reason

    def test_winning_trade_resets_streak_and_lifts_cooldown(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, threshold_losses=3)
        for _ in range(3):
            tracker.record_outcome("mean_reversion", -5.0)
        assert tracker.is_on_cooldown("mean_reversion")[0] is True
        tracker.record_outcome("mean_reversion", +12.0)
        active, reason = tracker.is_on_cooldown("mean_reversion")
        assert active is False
        assert reason is None

    def test_break_even_does_not_count_as_loss(self, tmp_db_path: str) -> None:
        """``pnl == 0`` (scratch trade) should be neutral.

        Treating a flat trade as a loss caused false cooldowns on
        commission-free SPY intraday after runs of break-even fills.
        Now neither resets nor advances the streak.
        """
        tracker = _make_tracker(tmp_db_path, threshold_losses=2)
        tracker.record_outcome("mean_reversion", -5.0)
        # Two scratch trades — should not accumulate
        tracker.record_outcome("mean_reversion", 0.0)
        tracker.record_outcome("mean_reversion", 0.0)
        active, _ = tracker.is_on_cooldown("mean_reversion")
        assert active is False
        # One more loss → 2 total, hits threshold
        tracker.record_outcome("mean_reversion", -3.0)
        active, _ = tracker.is_on_cooldown("mean_reversion")
        assert active is True

    def test_per_strategy_isolation(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, threshold_losses=2)
        tracker.record_outcome("mean_reversion", -1.0)
        tracker.record_outcome("mean_reversion", -1.0)
        assert tracker.is_on_cooldown("mean_reversion")[0] is True
        assert tracker.is_on_cooldown("breakout")[0] is False

    def test_expired_cooldown_auto_clears(self, tmp_db_path: str) -> None:
        """Auto-clear path uses the injected clock, not wall time."""
        # Tracker thinks "now" is one hour past the cooldown_until below.
        future: datetime = _T0 + timedelta(hours=1)
        tracker = _make_tracker(
            tmp_db_path, now_fn=lambda: future,
            threshold_losses=2, cooldown_minutes=60,
        )

        # Persist a tripped state with cooldown_until pinned to _T0
        # (already in the past from the tracker's perspective).
        conn: sqlite3.Connection = sqlite3.connect(tmp_db_path)
        try:
            repo.save_risk_state(
                conn, "loss_cooldown:mean_reversion",
                tripped=True, reason="forced",
                state={"consecutive_losses": 2, "cooldown_until": _T0.isoformat()},
            )
        finally:
            conn.close()

        active, reason = tracker.is_on_cooldown("mean_reversion")
        assert active is False
        assert reason is None

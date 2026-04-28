"""Tests for the per-strategy consecutive-loss cooldown tracker."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trading_bot.constants import TZ_EASTERN
from trading_bot.db import repository as repo
from trading_bot.execution.loss_cooldown import (
    LossCooldownConfig,
    LossCooldownTracker,
)

ET: ZoneInfo = TZ_EASTERN


def _make_tracker(db_path: str, **overrides) -> LossCooldownTracker:
    cfg = LossCooldownConfig(
        enabled=overrides.get("enabled", True),
        threshold_losses=overrides.get("threshold_losses", 3),
        cooldown_minutes=overrides.get("cooldown_minutes", 60),
    )
    return LossCooldownTracker(db_path=db_path, config=cfg)


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
        tracker = _make_tracker(tmp_db_path, threshold_losses=3, cooldown_minutes=30)
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

    def test_per_strategy_isolation(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, threshold_losses=2)
        tracker.record_outcome("mean_reversion", -1.0)
        tracker.record_outcome("mean_reversion", -1.0)
        assert tracker.is_on_cooldown("mean_reversion")[0] is True
        assert tracker.is_on_cooldown("breakout")[0] is False

    def test_expired_cooldown_auto_clears(self, tmp_db_path: str) -> None:
        tracker = _make_tracker(tmp_db_path, threshold_losses=2, cooldown_minutes=60)
        tracker.record_outcome("mean_reversion", -1.0)
        tracker.record_outcome("mean_reversion", -1.0)

        # Force the persisted cooldown_until into the past
        past: datetime = datetime.now(tz=ET) - timedelta(minutes=5)
        conn: sqlite3.Connection = sqlite3.connect(tmp_db_path)
        try:
            repo.save_risk_state(
                conn, "loss_cooldown:mean_reversion",
                tripped=True, reason="forced",
                state={"consecutive_losses": 2, "cooldown_until": past.isoformat()},
            )
        finally:
            conn.close()

        active, reason = tracker.is_on_cooldown("mean_reversion")
        assert active is False
        assert reason is None

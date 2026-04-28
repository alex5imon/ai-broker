"""Tests for the macro-event (FOMC) gating helpers."""

from __future__ import annotations

from datetime import date

from trading_bot.data.event_calendar import (
    fomc_size_multiplier,
    get_configured_fomc_dates,
    is_fomc_day,
)


def _cfg(**overrides):
    base = {
        "event_gate": {
            "enabled": True,
            "fomc_action": "skip",
            "fomc_size_multiplier": 0.5,
            "fomc_dates_2026": ["2026-01-28", "2026-03-18"],
        }
    }
    base["event_gate"].update(overrides)
    return base


class TestEventCalendar:
    def test_configured_dates_take_precedence(self) -> None:
        dates = get_configured_fomc_dates(_cfg(), 2026)
        assert dates == ["2026-01-28", "2026-03-18"]

    def test_fallback_when_year_missing_from_config(self) -> None:
        # Year 2026 has an in-package fallback list; remove from config to exercise it
        cfg = {"event_gate": {"enabled": True}}
        dates = get_configured_fomc_dates(cfg, 2026)
        assert "2026-01-28" in dates

    def test_is_fomc_day_true_on_announcement(self) -> None:
        assert is_fomc_day(date(2026, 3, 18), _cfg()) is True

    def test_is_fomc_day_false_on_non_meeting(self) -> None:
        assert is_fomc_day(date(2026, 3, 19), _cfg()) is False

    def test_skip_action_returns_zero(self) -> None:
        m = fomc_size_multiplier(date(2026, 3, 18), _cfg(fomc_action="skip"))
        assert m == 0.0

    def test_reduce_action_returns_multiplier(self) -> None:
        m = fomc_size_multiplier(
            date(2026, 3, 18),
            _cfg(fomc_action="reduce", fomc_size_multiplier=0.4),
        )
        assert m == 0.4

    def test_disabled_returns_one(self) -> None:
        m = fomc_size_multiplier(
            date(2026, 3, 18),
            _cfg(enabled=False),
        )
        assert m == 1.0

    def test_non_fomc_day_returns_one_when_enabled(self) -> None:
        m = fomc_size_multiplier(date(2026, 3, 19), _cfg())
        assert m == 1.0

    def test_unknown_action_defaults_to_skip(self) -> None:
        m = fomc_size_multiplier(
            date(2026, 3, 18),
            _cfg(fomc_action="bogus"),
        )
        assert m == 0.0

    def test_reduce_multiplier_clamped_to_unit_interval(self) -> None:
        m = fomc_size_multiplier(
            date(2026, 3, 18),
            _cfg(fomc_action="reduce", fomc_size_multiplier=2.0),
        )
        assert m == 1.0

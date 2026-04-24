"""Tests for SettlementTracker."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any

import pytest

from trading_bot.execution.settlement_tracker import SettlementTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tracker(raw_config: dict[str, Any], db_path: str) -> SettlementTracker:
    return SettlementTracker(raw_config, db_path)


# ---------------------------------------------------------------------------
# Record sale and settlement date calculation
# ---------------------------------------------------------------------------


class TestRecordSale:
    def test_record_sale_creates_settlement(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Selling creates a pending settlement record."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        sell_date = date(2026, 4, 14)  # Monday
        settle = tracker.record_sale(
            trade_id=1,
            ticker="PLTR",
            amount=500.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=sell_date,
        )
        assert settle > sell_date

        # Verify it's in the DB as unsettled
        pending = tracker.get_pending_settlements()
        assert any(p["ticker"] == "PLTR" for p in pending)

    def test_settlement_date_is_t1(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Settle date = T+1 business day."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        sell_date = date(2026, 4, 14)  # Tuesday
        settle = tracker.record_sale(
            trade_id=1,
            ticker="PLTR",
            amount=500.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=sell_date,
        )
        # T+1 = Wednesday 15
        assert settle == date(2026, 4, 15)

    def test_settlement_date_skips_weekend(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Sell on Friday → settlement on Monday (skips Sat/Sun)."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        sell_date = date(2026, 4, 17)  # Friday
        settle = tracker.record_sale(
            trade_id=None,
            ticker="LLOY",
            amount=1000.0,
            currency="GBP",
            fx_rate=1.0,
            sell_date=sell_date,
        )
        assert settle == date(2026, 4, 20)  # Monday

    def test_settlement_date_skips_uk_holiday(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Sell on day before US Good Friday → skips holiday."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        # 2026-04-02 is Thursday before Good Friday (2026-04-03)
        sell_date = date(2026, 4, 2)  # Thursday
        settle = tracker.record_sale(
            trade_id=None,
            ticker="BAC",
            amount=500.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=sell_date,
        )
        # T+1: Friday Apr 3 is Good Friday (US holiday) → skip to Monday Apr 6
        assert settle >= date(2026, 4, 6)

    def test_settlement_date_skips_us_holiday(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Sell day before US Good Friday → skips holiday."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        sell_date = date(2026, 4, 2)  # Thursday before Good Friday
        settle = tracker.record_sale(
            trade_id=None,
            ticker="PLTR",
            amount=500.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=sell_date,
        )
        # Good Friday Apr 3 is a US holiday
        assert settle >= date(2026, 4, 6)

    def test_t1_another_ticker(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Another US trade also settles T+1."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        sell_date = date(2026, 4, 14)
        settle = tracker.record_sale(
            trade_id=None,
            ticker="BAC",
            amount=300.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=sell_date,
        )
        assert settle == date(2026, 4, 15)

    def test_gbp_conversion_stored(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """USD sale is converted to GBP and stored in amount_gbp."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        tracker.record_sale(
            trade_id=1,
            ticker="PLTR",
            amount=125.0,
            currency="USD",
            fx_rate=1.25,
            sell_date=date(2026, 4, 14),
        )
        pending = tracker.get_pending_settlements()
        record = next(p for p in pending if p["ticker"] == "PLTR")
        # 125 USD / 1.25 = £100
        assert abs(record["amount_gbp"] - 100.0) < 0.01


# ---------------------------------------------------------------------------
# Available cash and pending total
# ---------------------------------------------------------------------------


class TestAvailableCash:
    def test_available_cash_excludes_pending(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """£50 pending settlement → pending_total returns £50."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        future = date.today() + timedelta(days=1)
        conn.execute(
            """INSERT INTO settlements (ticker, amount, currency, amount_gbp,
               sell_date, settle_date, settled)
               VALUES (?,?,?,?,?,?,0)""",
            ("PLTR", 50.0, "GBP", 50.0,
             date.today().isoformat(), future.isoformat()),
        )
        conn.commit()
        conn.close()

        pending = tracker.get_pending_total_gbp()
        assert abs(pending - 50.0) < 0.01

    def test_no_pending_returns_zero(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        tracker = _make_tracker(raw_config, tmp_db_path)
        assert tracker.get_pending_total_gbp() == 0.0


# ---------------------------------------------------------------------------
# Update settlements
# ---------------------------------------------------------------------------


class TestUpdateSettlements:
    def test_update_settlements_marks_settled(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Settlement with settle_date = today gets marked settled."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        today = date.today().isoformat()
        conn.execute(
            """INSERT INTO settlements (ticker, amount, currency, amount_gbp,
               sell_date, settle_date, settled)
               VALUES (?,?,?,?,?,?,0)""",
            ("PLTR", 100.0, "USD", 80.0, today, today),
        )
        conn.commit()
        conn.close()

        count = tracker.update_settlements()
        assert count >= 1

        # Verify the pending total is now 0
        assert tracker.get_pending_total_gbp() == 0.0

    def test_future_settlements_not_marked(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        """Settlement due tomorrow is not marked settled today."""
        tracker = _make_tracker(raw_config, tmp_db_path)
        conn = sqlite3.connect(tmp_db_path)
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        today = date.today().isoformat()
        conn.execute(
            """INSERT INTO settlements (ticker, amount, currency, amount_gbp,
               sell_date, settle_date, settled)
               VALUES (?,?,?,?,?,?,0)""",
            ("PLTR", 100.0, "USD", 80.0, today, tomorrow),
        )
        conn.commit()
        conn.close()

        count = tracker.update_settlements()
        assert count == 0
        # Still pending
        assert tracker.get_pending_total_gbp() > 0.0


# ---------------------------------------------------------------------------
# Business day helper
# ---------------------------------------------------------------------------


class TestBusinessDay:
    def test_weekday_is_business_day(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        tracker = _make_tracker(raw_config, tmp_db_path)
        # Monday 2026-04-13
        assert tracker.is_business_day(date(2026, 4, 13)) is True

    def test_saturday_not_business_day(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        tracker = _make_tracker(raw_config, tmp_db_path)
        assert tracker.is_business_day(date(2026, 4, 18)) is False  # Saturday

    def test_holiday_not_business_day(
        self, raw_config: dict[str, Any], tmp_db_path: str
    ) -> None:
        tracker = _make_tracker(raw_config, tmp_db_path)
        # 2026-04-03 is Good Friday
        assert tracker.is_business_day(date(2026, 4, 3)) is False

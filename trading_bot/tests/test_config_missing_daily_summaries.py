"""Tests for Config.detect_missing_daily_summaries.

Catches the failure pattern where the bot doesn't tick after wind-down
ended on a given day — _save_daily_summary needs a tick to fire after
~16:10 ET to write the row. If the cron skips, GHA is down, or the
bot crashes during wind-down, the row is missing and postmortem
reports for that day will be empty.
"""

from __future__ import annotations

from datetime import date

import pytest

from trading_bot.config import Config


def _build_config() -> Config:
    """Construct a Config that knows weekends are non-trading days."""
    raw = {
        # 2026 holidays — empty for this test (we only need
        # weekend-detection logic, which is hardcoded)
        "holidays": {"us_2026": []},
    }
    return Config(raw)


def _insert_summary(conn, d: str) -> None:
    conn.execute(
        "INSERT INTO daily_summaries (date, account_equity_usd, phase) VALUES (?, 1000, 1)",
        (d,),
    )


@pytest.mark.unit
def test_returns_empty_when_all_trading_days_have_rows(tmp_db_path):
    """5 lookback days, all trading days, all rows present → no warnings."""
    import sqlite3

    cfg = _build_config()
    today = date(2026, 4, 30)  # Thursday
    # Lookback 5 days from 2026-04-30 → 04-29 (Wed), 04-28 (Tue),
    # 04-27 (Mon), 04-26 (Sun, skip), 04-25 (Sat, skip)
    # Trading days: 04-27, 04-28, 04-29
    conn = sqlite3.connect(tmp_db_path)
    for d_str in ("2026-04-27", "2026-04-28", "2026-04-29"):
        _insert_summary(conn, d_str)
    conn.commit()
    conn.close()

    out = cfg.detect_missing_daily_summaries(
        tmp_db_path, lookback_days=5, today=today,
    )
    assert out == []


@pytest.mark.unit
def test_warns_for_missing_trading_day(tmp_db_path):
    """One trading day in window has no row → exactly one warning."""
    import sqlite3

    cfg = _build_config()
    today = date(2026, 4, 30)
    conn = sqlite3.connect(tmp_db_path)
    # Insert 04-27 + 04-29 but skip 04-28
    for d_str in ("2026-04-27", "2026-04-29"):
        _insert_summary(conn, d_str)
    conn.commit()
    conn.close()

    out = cfg.detect_missing_daily_summaries(
        tmp_db_path, lookback_days=5, today=today,
    )
    assert len(out) == 1
    assert "2026-04-28" in out[0]


@pytest.mark.unit
def test_does_not_warn_for_today(tmp_db_path):
    """Today is excluded — bot hasn't written today's summary yet."""
    cfg = _build_config()
    today = date(2026, 4, 30)
    # No summaries inserted at all — but today should not appear
    out = cfg.detect_missing_daily_summaries(
        tmp_db_path, lookback_days=5, today=today,
    )
    for warning in out:
        assert today.isoformat() not in warning


@pytest.mark.unit
def test_skips_weekends(tmp_db_path):
    """Saturday and Sunday are not trading days — never appear in
    warnings."""
    cfg = _build_config()
    today = date(2026, 4, 30)  # Thursday
    out = cfg.detect_missing_daily_summaries(
        tmp_db_path, lookback_days=5, today=today,
    )
    # Nothing in DB → all 3 trading days in window are missing
    assert len(out) == 3
    weekend_dates = {"2026-04-25", "2026-04-26"}
    for warning in out:
        for wd in weekend_dates:
            assert wd not in warning


@pytest.mark.unit
def test_skips_holidays(tmp_path):
    """Configured US holidays are not trading days."""
    import sqlite3

    raw = {"holidays": {"us_2026": ["2026-04-29"]}}  # mark Wed as holiday
    cfg = Config(raw)
    today = date(2026, 4, 30)
    db = str(tmp_path / "x.db")
    # Build empty DB
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE daily_summaries (date TEXT PRIMARY KEY, "
        "account_equity_usd REAL, phase INTEGER)"
    )
    conn.commit()
    conn.close()

    out = cfg.detect_missing_daily_summaries(db, lookback_days=5, today=today)
    # Without 04-29 (now a holiday), only 04-27 and 04-28 should warn
    assert len(out) == 2
    for warning in out:
        assert "2026-04-29" not in warning


@pytest.mark.unit
def test_returns_empty_on_db_failure(tmp_path):
    """DB unreachable → empty list (not crash). Bot startup must
    survive a corrupt or missing DB file."""
    cfg = _build_config()
    bad_path = str(tmp_path)  # directory → connect ok, query fails
    out = cfg.detect_missing_daily_summaries(
        bad_path, lookback_days=5, today=date(2026, 4, 30),
    )
    assert out == []


@pytest.mark.unit
def test_lookback_days_bounds_the_window(tmp_db_path):
    """lookback_days=2 from Thursday → only Wed + Tue checked."""
    import sqlite3

    cfg = _build_config()
    today = date(2026, 4, 30)  # Thursday
    conn = sqlite3.connect(tmp_db_path)
    # Insert nothing
    conn.commit()
    conn.close()

    out = cfg.detect_missing_daily_summaries(
        tmp_db_path, lookback_days=2, today=today,
    )
    # 04-29 (Wed) + 04-28 (Tue) → 2 warnings
    assert len(out) == 2
    joined = " | ".join(out)
    assert "2026-04-29" in joined
    assert "2026-04-28" in joined
    # 04-27 (Mon, outside window) must NOT appear
    assert "2026-04-27" not in joined

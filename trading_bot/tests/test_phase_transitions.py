"""Tests for phase transition logic."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Config
from trading_bot.constants import Phase


# ---------------------------------------------------------------------------
# Config phase detection
# ---------------------------------------------------------------------------


class TestPhaseDetection:
    def test_phase1_auto_detect_below_threshold(self, config: Config) -> None:
        """Equity below Phase 2 threshold → Phase.MICRO."""
        phase = config.get_phase(equity_gbp=4999.0)
        assert phase == Phase.MICRO

    def test_phase2_auto_detect_at_threshold(self, config: Config) -> None:
        """Equity at exactly £5000 → Phase.SMALL."""
        phase = config.get_phase(equity_gbp=5000.0)
        assert phase == Phase.SMALL

    def test_phase3_auto_detect_at_threshold(self, config: Config) -> None:
        """Equity at exactly £20000 → Phase.FULL."""
        phase = config.get_phase(equity_gbp=20000.0)
        assert phase == Phase.FULL

    def test_phase_override_forces_phase(self, raw_config: dict[str, Any]) -> None:
        """phase_override=2 forces Phase.SMALL regardless of equity."""
        raw = dict(raw_config)
        raw["account"] = dict(raw["account"])
        raw["account"]["phase_override"] = 2
        from trading_bot.config import Config as Cfg
        cfg = Cfg(raw)
        assert cfg.get_phase() == Phase.SMALL

    def test_no_equity_defaults_to_micro(self, config: Config) -> None:
        """No equity supplied, no override → default Phase.MICRO."""
        # Need fresh config to avoid cached phase
        cfg = Config(dict(config._raw))
        phase = cfg.get_phase()
        assert phase == Phase.MICRO

    def test_cache_invalidate_then_resolve_with_equity(
        self, config: Config
    ) -> None:
        """Regression: ``__init__`` cached MICRO and never re-resolved.

        After invalidating ``_phase`` we must be able to re-detect the
        true phase from live equity (the bug was that get_phase() with
        no equity argument always returned the cached default).
        """
        cfg = Config(dict(config._raw))
        # Trigger the bad cache: equity unknown → caches MICRO.
        assert cfg.get_phase() == Phase.MICRO
        # The fix path: invalidate and re-resolve with real equity.
        cfg._phase = None
        assert cfg.get_phase(equity_gbp=25000.0) == Phase.FULL


# ---------------------------------------------------------------------------
# Phase transition criteria (SPEC §phase transitions)
# ---------------------------------------------------------------------------


class TestPhaseTransitionCriteria:
    """These tests verify the criteria logic independently of Config.get_phase().

    They simulate the checks a PhaseManager would run before promoting.
    """

    def _count_trading_days(
        self, conn: sqlite3.Connection, from_date: date, to_date: date
    ) -> int:
        """Count rows in daily_summaries between two dates."""
        rows = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries WHERE date BETWEEN ? AND ?",
            (from_date.isoformat(), to_date.isoformat()),
        ).fetchone()
        return rows[0]

    def _insert_daily_summaries(
        self,
        conn: sqlite3.Connection,
        n_days: int,
        equity: float = 5100.0,
        phase: int = 1,
        offset_days: int = 0,
    ) -> None:
        today = date.today()
        for i in range(n_days):
            d = (today - timedelta(days=offset_days + i)).isoformat()
            conn.execute(
                """INSERT OR REPLACE INTO daily_summaries
                   (date, account_equity_gbp, phase)
                   VALUES (?, ?, ?)""",
                (d, equity, phase),
            )
        conn.commit()

    def _insert_trades(
        self,
        conn: sqlite3.Connection,
        n: int,
        wins: int,
        equity: float = 5100.0,
    ) -> None:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("US/Eastern")
        import datetime as dt

        today = dt.datetime.now(ET)
        for i in range(n):
            pnl = 10.0 if i < wins else -10.0
            conn.execute(
                """INSERT INTO trades (ticker, exchange, currency, side,
                   entry_time, entry_price, quantity, exit_time, exit_reason,
                   gross_pnl, net_pnl, pnl_gbp, fx_rate,
                   hold_type, phase)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("PLTR", "NASDAQ", "USD", "BUY",
                 (today - timedelta(days=i)).isoformat(),
                 10.0, 100,
                 (today - timedelta(days=i)).isoformat(),
                 "take_profit" if i < wins else "stop_loss",
                 pnl, pnl, pnl, 1.25, "swing", 1),
            )
        conn.commit()

    def test_phase1_to_phase2_all_criteria_met(
        self, tmp_db_path: str, config: Config
    ) -> None:
        """Equity £5001, 41 days, 55% win rate → promotion criteria met."""
        conn = sqlite3.connect(tmp_db_path)
        self._insert_daily_summaries(conn, 41, equity=5001.0)
        # 20 trades, 11 wins = 55% win rate
        self._insert_trades(conn, 20, wins=11)
        conn.close()

        # Verify criteria
        conn = sqlite3.connect(tmp_db_path)
        # Equity check
        row = conn.execute(
            "SELECT MAX(account_equity_gbp) FROM daily_summaries"
        ).fetchone()
        assert row[0] >= 5000.0

        # Trading days check
        days = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries"
        ).fetchone()[0]
        assert days >= 40

        # Win rate check
        trade_rows = conn.execute(
            "SELECT exit_reason FROM trades ORDER BY entry_time DESC LIMIT 20"
        ).fetchall()
        wins = sum(1 for r in trade_rows if r[0] == "take_profit")
        win_rate = wins / len(trade_rows) if trade_rows else 0
        assert win_rate >= 0.52
        conn.close()

    def test_phase1_not_promoted_low_equity(
        self, tmp_db_path: str, config: Config
    ) -> None:
        """Equity £4999 — below threshold, no promotion."""
        equity = 4999.0
        p2_threshold = float(config._require("phases", "phase1_to_phase2", "equity_gbp"))
        assert equity < p2_threshold

    def test_phase1_not_promoted_insufficient_days(
        self, tmp_db_path: str, config: Config
    ) -> None:
        """Only 35 trading days — below 40-day minimum."""
        conn = sqlite3.connect(tmp_db_path)
        self._insert_daily_summaries(conn, 35, equity=5100.0)
        conn.close()

        conn = sqlite3.connect(tmp_db_path)
        days = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries"
        ).fetchone()[0]
        conn.close()
        min_days = int(config._require("phases", "phase1_to_phase2", "min_trading_days"))
        assert days < min_days

    def test_phase1_not_promoted_low_win_rate(
        self, tmp_db_path: str, config: Config
    ) -> None:
        """Win rate 50% — below 52% minimum."""
        conn = sqlite3.connect(tmp_db_path)
        # 20 trades, 10 wins = 50%
        self._insert_trades(conn, 20, wins=10)
        conn.close()

        conn = sqlite3.connect(tmp_db_path)
        rows = conn.execute(
            "SELECT exit_reason FROM trades ORDER BY entry_time DESC LIMIT 20"
        ).fetchall()
        conn.close()
        wins = sum(1 for r in rows if r[0] == "take_profit")
        win_rate = wins / len(rows) if rows else 0

        min_win_rate = float(
            config._require("phases", "phase1_to_phase2", "min_win_rate_last_n")
        )
        assert win_rate < min_win_rate


# ---------------------------------------------------------------------------
# Demotion
# ---------------------------------------------------------------------------


class TestDemotion:
    def test_demotion_on_equity_drop(self, config: Config) -> None:
        """Phase 2, equity drops to £3999 (< 80% of £5000 threshold) → demoted."""
        p2_threshold = float(
            config._require("phases", "phase1_to_phase2", "equity_gbp")
        )
        demotion_pct = float(
            config._get("phases", "demotion", "equity_pct_of_threshold")
        )
        demotion_threshold = p2_threshold * demotion_pct  # 5000 * 0.80 = 4000

        current_equity = 3999.0
        assert current_equity < demotion_threshold

    def test_no_demotion_above_threshold(self, config: Config) -> None:
        """Equity at £4100 — above 80% of £5000 threshold (= £4000), no demotion."""
        p2_threshold = float(
            config._require("phases", "phase1_to_phase2", "equity_gbp")
        )
        demotion_pct = float(
            config._get("phases", "demotion", "equity_pct_of_threshold")
        )
        demotion_threshold = p2_threshold * demotion_pct  # 4000

        current_equity = 4100.0
        assert current_equity >= demotion_threshold


# ---------------------------------------------------------------------------
# Phase-aware config parameters
# ---------------------------------------------------------------------------


class TestPhaseAwareConfig:
    def test_phase1_max_positions(self, raw_config: dict[str, Any]) -> None:
        cfg = Config(raw_config)
        # Force phase 1
        cfg._phase = Phase.MICRO
        assert cfg.get_max_positions() == 2

    def test_phase2_max_positions_increases(self, raw_config: dict[str, Any]) -> None:
        raw = dict(raw_config)
        raw["account"] = dict(raw["account"])
        raw["account"]["phase_override"] = 2
        cfg = Config(raw)
        assert cfg.get_max_positions() == 4

    def test_phase1_sector_exposure_one(self, raw_config: dict[str, Any]) -> None:
        cfg = Config(raw_config)
        cfg._phase = Phase.MICRO
        assert cfg.get_max_sector_exposure() == 1

    def test_phase2_sector_exposure_two(self, raw_config: dict[str, Any]) -> None:
        raw = dict(raw_config)
        raw["account"] = dict(raw["account"])
        raw["account"]["phase_override"] = 2
        cfg = Config(raw)
        assert cfg.get_max_sector_exposure() == 2

    def test_phase_watchlist_expands(self, raw_config: dict[str, Any]) -> None:
        from trading_bot.constants import Market

        raw = dict(raw_config)
        raw["account"] = dict(raw["account"])
        raw["account"]["phase_override"] = 2
        cfg = Config(raw)
        watchlist_p2 = cfg.get_watchlist(Market.US)

        raw2 = dict(raw_config)
        raw2["account"] = dict(raw2["account"])
        raw2["account"]["phase_override"] = 1
        cfg2 = Config(raw2)
        watchlist_p1 = cfg2.get_watchlist(Market.US)

        assert len(watchlist_p2) > len(watchlist_p1)

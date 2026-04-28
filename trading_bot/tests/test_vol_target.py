"""Tests for the vol-target sizing helper."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trading_bot.execution.vol_target import (
    VolTargetResult,
    load_recent_trade_returns,
    vol_target_multiplier,
)

ET = ZoneInfo("US/Eastern")


class TestVolTargetMultiplier:
    def test_disabled_when_target_zero(self) -> None:
        result = vol_target_multiplier(
            [0.01, -0.02, 0.015] * 5, target_annual_vol=0.0,
        )
        assert result.multiplier == 1.0
        assert result.reason.startswith("target_vol")

    def test_returns_one_below_min_sample(self) -> None:
        result = vol_target_multiplier(
            [0.01, -0.02], target_annual_vol=0.20, min_sample=10,
        )
        assert result.multiplier == 1.0
        assert "sample" in result.reason

    def test_high_realized_vol_scales_down(self) -> None:
        # Realized per-trade vol of ~3% with target 20%/sqrt(252) ≈ 1.26%
        # → multiplier should be < 1 and clamped at 0.5.
        returns = [0.05, -0.05] * 10
        result = vol_target_multiplier(
            returns, target_annual_vol=0.20,
            expected_trades_per_year=252, min_multiplier=0.5, max_multiplier=1.5,
        )
        assert result.multiplier == 0.5
        assert result.realized_per_trade_vol is not None
        assert result.realized_per_trade_vol > 0.04

    def test_low_realized_vol_scales_up(self) -> None:
        # Tiny per-trade vol with a relatively high target → clamp at max.
        returns = [0.0001, -0.0001] * 10
        result = vol_target_multiplier(
            returns, target_annual_vol=0.20,
            expected_trades_per_year=252, min_multiplier=0.5, max_multiplier=1.5,
        )
        assert result.multiplier == 1.5

    def test_realized_vol_zero_returns_one(self) -> None:
        returns = [0.0] * 20
        result = vol_target_multiplier(
            returns, target_annual_vol=0.20,
        )
        assert result.multiplier == 1.0
        assert result.reason == "realized_vol=0"

    def test_unbounded_when_within_band(self) -> None:
        # Construct returns so realized per-trade vol ≈ target.
        target_per_trade = 0.20 / math.sqrt(252)
        # pstdev of [+x, -x, +x, -x, ...] with x=target equals target.
        returns = [target_per_trade, -target_per_trade] * 10
        result = vol_target_multiplier(
            returns, target_annual_vol=0.20,
        )
        # Multiplier should be ≈ 1 and inside the band.
        assert result.multiplier == pytest.approx(1.0, rel=0.05)


class TestLoadRecentTradeReturns:
    def _create_trades_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY,
                strategy_id TEXT,
                ticker TEXT,
                entry_price REAL,
                quantity REAL,
                exit_price REAL,
                exit_time TEXT
            )"""
        )
        conn.commit()

    def test_returns_empty_when_table_missing(self, tmp_path) -> None:
        db = tmp_path / "missing.db"
        # Touch an empty db so the connection succeeds but trades is missing.
        sqlite3.connect(str(db)).close()
        assert load_recent_trade_returns(str(db), "mr", lookback=10) == []

    def test_orders_oldest_first(self, tmp_path) -> None:
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(str(db))
        self._create_trades_table(conn)
        for i, (entry, exit_p, ts) in enumerate([
            (100.0, 102.0, "2026-01-01T10:00"),  # +2%
            (100.0, 99.0, "2026-01-02T10:00"),   # -1%
            (100.0, 103.0, "2026-01-03T10:00"),  # +3%
        ]):
            conn.execute(
                "INSERT INTO trades (strategy_id, ticker, entry_price, "
                "quantity, exit_price, exit_time) VALUES (?,?,?,?,?,?)",
                ("mr", "SPY", entry, 1.0, exit_p, ts),
            )
        conn.commit()
        conn.close()

        out = load_recent_trade_returns(str(db), "mr", lookback=10)
        assert len(out) == 3
        # Oldest first.
        assert out[0] == pytest.approx(0.02, rel=1e-3)
        assert out[1] == pytest.approx(-0.01, rel=1e-3)
        assert out[2] == pytest.approx(0.03, rel=1e-3)

    def test_filters_by_strategy(self, tmp_path) -> None:
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(str(db))
        self._create_trades_table(conn)
        conn.execute(
            "INSERT INTO trades (strategy_id, entry_price, quantity, "
            "exit_price, exit_time) VALUES ('mr', 100, 1, 102, '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO trades (strategy_id, entry_price, quantity, "
            "exit_price, exit_time) VALUES ('breakout', 100, 1, 90, '2026-01-02')"
        )
        conn.commit()
        conn.close()

        out = load_recent_trade_returns(str(db), "mr", lookback=10)
        assert len(out) == 1
        assert out[0] == pytest.approx(0.02, rel=1e-3)

    def test_skips_unclosed_trades(self, tmp_path) -> None:
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(str(db))
        self._create_trades_table(conn)
        conn.execute(
            "INSERT INTO trades (strategy_id, entry_price, quantity, "
            "exit_price, exit_time) VALUES ('mr', 100, 1, NULL, '2026-01-01')"
        )
        conn.commit()
        conn.close()

        assert load_recent_trade_returns(str(db), "mr", lookback=10) == []


class TestStrategyBaseVolMultiplier:
    """Live-path integration: vol_multiplier() on StrategyBase reads the
    real trades table populated through the live schema."""

    def _seed_trades(
        self, conn: sqlite3.Connection, strategy_id: str, returns: list[float]
    ) -> None:
        from datetime import datetime, timedelta
        base = datetime(2026, 1, 1, 10, 0, 0)
        for i, r in enumerate(returns):
            entry = 100.0
            qty = 1
            exit_price = entry * (1.0 + r)
            ts = (base + timedelta(hours=i)).isoformat()
            conn.execute(
                """INSERT INTO trades (ticker, exchange, currency, side,
                   entry_time, entry_price, quantity, exit_time, exit_price,
                   hold_type, phase, strategy_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("SPY", "NASDAQ", "USD", "BUY",
                 ts, entry, qty, ts, exit_price,
                 "intraday", 1, strategy_id),
            )
        conn.commit()

    def test_returns_one_when_db_path_missing(self) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        s = MeanReversionStrategy(config={"use_risk_sizing": True})
        assert s.vol_multiplier() == 1.0

    def test_returns_one_when_target_zero(self, tmp_db_path: str) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        s = MeanReversionStrategy(
            config={"use_risk_sizing": True},
            db_path=tmp_db_path,
            vol_target_config={"annual_vol_pct": 0.0},
        )
        assert s.vol_multiplier() == 1.0

    def test_scales_down_on_high_vol(self, tmp_db_path: str) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        # Big swings → realized per-trade vol way above target.
        conn = sqlite3.connect(tmp_db_path)
        self._seed_trades(conn, "mean_reversion", [0.05, -0.05] * 10)
        conn.close()

        s = MeanReversionStrategy(
            config={"use_risk_sizing": True},
            db_path=tmp_db_path,
            vol_target_config={
                "annual_vol_pct": 0.20,
                "lookback_trades": 30,
                "min_multiplier": 0.5,
                "max_multiplier": 1.5,
            },
        )
        assert s.vol_multiplier() == 0.5

    def test_isolated_per_strategy(self, tmp_db_path: str) -> None:
        from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
        from trading_bot.strategy.strategies.breakout import BreakoutStrategy
        # mean_reversion: high vol; breakout: tiny vol.
        conn = sqlite3.connect(tmp_db_path)
        self._seed_trades(conn, "mean_reversion", [0.05, -0.05] * 10)
        self._seed_trades(conn, "breakout", [0.0001, -0.0001] * 10)
        conn.close()

        cfg = {
            "annual_vol_pct": 0.20,
            "lookback_trades": 30,
            "min_multiplier": 0.5,
            "max_multiplier": 1.5,
        }
        mr = MeanReversionStrategy(
            config={"use_risk_sizing": True},
            db_path=tmp_db_path, vol_target_config=cfg,
        )
        bo = BreakoutStrategy(
            config={"use_risk_sizing": True},
            db_path=tmp_db_path, vol_target_config=cfg,
        )
        assert mr.vol_multiplier() == 0.5
        assert bo.vol_multiplier() == 1.5

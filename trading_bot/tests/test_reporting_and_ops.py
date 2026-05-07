"""Tests for Tier 4: performance, notifier, health/server, log_setup."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from trading_bot.constants import TZ_EASTERN
from trading_bot.db import repository
from trading_bot.health.server import HealthServer
from trading_bot.log_setup import setup_logging
from trading_bot.notifications.notifier import Notifier
from trading_bot.reporting.performance import PerformanceCalculator

ET = TZ_EASTERN


# ===========================================================================
# PerformanceCalculator
# ===========================================================================


def _seed_trade(
    db_path: str,
    *,
    ticker: str = "SPY",
    exit_time: str | None = None,
    pnl_usd: float = 1.0,
    gross_pnl: float = 1.0,
    currency: str = "USD",
    side: str = "BUY",
    quantity: int = 10,
    **_legacy: Any,
) -> None:
    if exit_time is None:
        exit_time = datetime.now(tz=ET).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO trades (
                ticker, exchange, currency, side, entry_time, exit_time,
                entry_price, exit_price, quantity, hold_type, phase,
                signal_price, gross_pnl, pnl_usd
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ticker, "US", currency, side,
                exit_time, exit_time, 100.0, 101.0, quantity, "intraday", 1,
                100.0, gross_pnl, pnl_usd,
            ),
        )
        conn.commit()


def _seed_daily_summary(
    db_path: str, *, date_str: str, total_trades: int = 1, wins: int = 1,
    losses: int = 0, gross_pnl_usd: float = 1.0, commissions_usd: float = 0.0,
    net_pnl_usd: float = 1.0, account_equity_usd: float = 1000.0,
    phase: int = 1,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO daily_summaries (
                date, total_trades, wins, losses, gross_pnl_usd,
                commissions_usd, net_pnl_usd, win_rate, account_equity_usd, phase
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                date_str, total_trades, wins, losses,
                gross_pnl_usd, commissions_usd, net_pnl_usd,
                wins / max(total_trades, 1), account_equity_usd, phase,
            ),
        )
        conn.commit()


def test_daily_metrics_no_trades(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    today = date.today().isoformat()
    m = pc.calculate_daily_metrics(today)
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0.0
    assert m["profit_factor"] == 0.0


def test_daily_metrics_with_wins_and_losses(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    today_str = datetime.now(tz=ET).date().isoformat()
    today_iso = datetime.now(tz=ET).isoformat()
    # Two wins, one loss
    _seed_trade(tmp_db_path, ticker="A", exit_time=today_iso, pnl_usd=10.0, gross_pnl=12.5)
    _seed_trade(tmp_db_path, ticker="B", exit_time=today_iso, pnl_usd=5.0, gross_pnl=6.25)
    _seed_trade(tmp_db_path, ticker="C", exit_time=today_iso, pnl_usd=-3.0, gross_pnl=-3.75)
    m = pc.calculate_daily_metrics(today_str)
    assert m["total_trades"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1
    assert m["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert m["profit_factor"] == pytest.approx(15.0 / 3.0, rel=1e-3)
    assert m["largest_win_usd"] == 10.0
    assert m["largest_loss_usd"] == -3.0


def test_daily_metrics_usd_passthrough(tmp_db_path):
    """Account is USD-only — gross_pnl flows through unchanged."""
    pc = PerformanceCalculator(tmp_db_path)
    today_str = datetime.now(tz=ET).date().isoformat()
    today_iso = datetime.now(tz=ET).isoformat()
    _seed_trade(
        tmp_db_path, ticker="X", exit_time=today_iso,
        pnl_usd=10.0, gross_pnl=10.0, currency="USD",
    )
    m = pc.calculate_daily_metrics(today_str)
    assert m["gross_pnl_usd"] == 10.0


def test_daily_metrics_late_evening_ET_exit_lands_on_ET_date(tmp_db_path):
    """Regression: trades closed in the 20:00-23:59 ET window must land on
    the ET-local trading date.

    This is the failure mode behind ai-broker#40. ``exit_time`` is stored
    as ET-aware ISO (``-04:00`` offset). SQLite's built-in ``date(t)``
    silently converts to UTC before extracting the date, so 22:00 ET on
    2026-05-06 (= 02:00 UTC on 2026-05-07) gets bucketed under the wrong
    day and silently drops out of that day's metrics.

    The fix is ``substr(exit_time, 1, 10)`` — first 10 chars of an
    ET-aware ISO are always the ET-local YYYY-MM-DD.
    """
    pc = PerformanceCalculator(tmp_db_path)
    # 22:00 ET on a fixed date — well past the 20:00 ET threshold where
    # the UTC-conversion bug fires.
    et_date_str = "2026-05-06"
    late_et_iso = f"{et_date_str}T22:00:00-04:00"

    _seed_trade(tmp_db_path, ticker="A", exit_time=late_et_iso,
                pnl_usd=10.0, gross_pnl=10.0)
    _seed_trade(tmp_db_path, ticker="B", exit_time=late_et_iso,
                pnl_usd=-3.0, gross_pnl=-3.0)

    m = pc.calculate_daily_metrics(et_date_str)
    assert m["total_trades"] == 2, (
        "Late-ET-evening trades must count under their ET-local date. "
        "If this fails, the read query likely went back to using "
        "SQLite's date(...) on a tz-aware ISO string."
    )
    assert m["wins"] == 1
    assert m["losses"] == 1


_ET_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?"            # optional microseconds
    r"[+\-]\d{2}:\d{2}$"     # ±HH:MM offset (never naive, never Z)
)


def test_repository_now_eastern_iso_emits_et_aware_iso():
    """The canonical timestamp helper used by every live writer must
    emit ET-aware ISO (``YYYY-MM-DDTHH:MM:SS[.ffffff]±HH:MM``). This is
    the anchor for the ``substr(exit_time, 1, 10)`` ET-date extraction
    in ``performance.py`` — if this drifts (e.g. someone "simplifies"
    it to ``strftime('%Y-%m-%d %H:%M:%S')``), every read query that
    relies on the offset suffix breaks silently and this test fails
    fast.
    """
    # Sample a few times — the offset is constant within a session but
    # microseconds vary, so a single call can mask a regex bug that
    # only triggers when the fractional-second branch is hit.
    samples = [repository._now_eastern_iso() for _ in range(5)]
    for ts in samples:
        assert _ET_ISO_RE.match(ts), (
            f"repository._now_eastern_iso() emitted {ts!r}, which is not "
            "ET-aware ISO. See performance.py module docstring for the "
            "invariant."
        )


def test_trades_table_format_invariant_round_trip(tmp_db_path):
    """Round-trip a value emitted by ``_now_eastern_iso`` through SQLite
    and read it back. SQLite stores TEXT verbatim, so this guards
    against any future change that converts/normalises the column on
    write or read (e.g. a wrapper function, a custom adapter, or a
    schema change to ``DATETIME`` type affinity that would coerce
    the stored value).
    """
    et_iso = repository._now_eastern_iso()
    _seed_trade(tmp_db_path, ticker="A", exit_time=et_iso,
                pnl_usd=1.0, gross_pnl=1.0)

    with sqlite3.connect(tmp_db_path) as conn:
        rows = conn.execute(
            "SELECT entry_time, exit_time FROM trades "
            "WHERE entry_time IS NOT NULL AND exit_time IS NOT NULL"
        ).fetchall()

    assert rows, "expected the seeded row"
    for entry_time, exit_time in rows:
        assert _ET_ISO_RE.match(entry_time), (
            f"entry_time {entry_time!r} did not survive SQLite round-trip "
            "as ET-aware ISO — see performance.py module docstring."
        )
        assert _ET_ISO_RE.match(exit_time), (
            f"exit_time {exit_time!r} did not survive SQLite round-trip "
            "as ET-aware ISO — see performance.py module docstring."
        )


def test_intraday_drawdown():
    trades = [
        {"pnl_usd": 10.0},   # cumulative 10, peak 10
        {"pnl_usd": -5.0},   # cumulative 5, drawdown 5/10=50%
        {"pnl_usd": -2.0},   # cumulative 3, drawdown 7/10=70%
        {"pnl_usd": 100.0},  # cumulative 103, peak 103
    ]
    dd = PerformanceCalculator._calculate_intraday_drawdown(trades)
    assert dd == pytest.approx(0.7)


def test_intraday_drawdown_empty():
    assert PerformanceCalculator._calculate_intraday_drawdown([]) == 0.0


def test_period_metrics_empty_returns_skeleton(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    m = pc.calculate_period_metrics("2026-01-01", "2026-01-31")
    assert m["trading_days"] == 0
    assert m["total_trades"] == 0


def test_period_metrics_aggregates(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    _seed_daily_summary(
        tmp_db_path, date_str="2026-04-01", total_trades=3, wins=2, losses=1,
        gross_pnl_usd=12.0, net_pnl_usd=11.50,
    )
    _seed_daily_summary(
        tmp_db_path, date_str="2026-04-02", total_trades=2, wins=1, losses=1,
        gross_pnl_usd=5.0, net_pnl_usd=4.50,
    )
    # Use the production ET-aware ISO format (YYYY-MM-DDTHH:MM:SS-HH:MM)
    # to match the invariant asserted by
    # ``test_trades_table_format_invariant_live_writers``.
    _seed_trade(tmp_db_path, ticker="A", exit_time="2026-04-01T10:00:00-04:00", pnl_usd=10.0)
    _seed_trade(tmp_db_path, ticker="B", exit_time="2026-04-01T11:00:00-04:00", pnl_usd=2.0)
    _seed_trade(tmp_db_path, ticker="C", exit_time="2026-04-01T12:00:00-04:00", pnl_usd=-1.0)
    _seed_trade(tmp_db_path, ticker="D", exit_time="2026-04-02T10:00:00-04:00", pnl_usd=5.0)
    _seed_trade(tmp_db_path, ticker="E", exit_time="2026-04-02T11:00:00-04:00", pnl_usd=-1.0)

    m = pc.calculate_period_metrics("2026-04-01", "2026-04-02")
    assert m["trading_days"] == 2
    assert m["total_trades"] == 5
    assert m["best_day"]["date"] == "2026-04-01"
    assert m["worst_day"]["date"] == "2026-04-02"


def test_sharpe_ratio_zero_for_short_series():
    pc = PerformanceCalculator(":memory:")
    assert pc.calculate_sharpe_ratio([0.01]) == 0.0
    assert pc.calculate_sharpe_ratio([]) == 0.0


def test_sharpe_ratio_zero_std_returns_zero():
    pc = PerformanceCalculator(":memory:")
    assert pc.calculate_sharpe_ratio([0.005, 0.005, 0.005]) == 0.0


def test_sharpe_ratio_positive_returns():
    pc = PerformanceCalculator(":memory:")
    daily = [0.01, 0.02, -0.005, 0.008, 0.012]
    sharpe = pc.calculate_sharpe_ratio(daily, risk_free_rate=0.02)
    assert isinstance(sharpe, float)


def test_get_equity_curve_empty(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    assert pc.get_equity_curve() == []


def test_get_equity_curve_chronological(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    _seed_daily_summary(tmp_db_path, date_str="2026-04-01", net_pnl_usd=5.0)
    _seed_daily_summary(tmp_db_path, date_str="2026-04-02", net_pnl_usd=3.0)
    curve = pc.get_equity_curve(n_days=10)
    assert curve[0]["date"] == "2026-04-01"
    assert curve[-1]["date"] == "2026-04-02"
    # Cumulative should sum
    assert curve[-1]["cumulative_pnl_usd"] == pytest.approx(8.0)


# ===========================================================================
# Notifier
# ===========================================================================


def _notif_config(**overrides) -> dict:
    base = {
        "notifications": {
            "ntfy_server": "https://ntfy.sh",
            "ntfy_topic": "test-topic",
            "rate_limit_per_minute": 5,
            "max_retries": 1,
            "retry_interval_seconds": 0,
            "fallback_to_osascript": False,
            "priorities": {
                "trade_entry": 3, "position_closed": 3, "stop_loss_hit": 4,
                "daily_summary": 3, "phase0_cleanup": 4, "phase_transition": 4,
                "gateway_disconnect": 5, "gateway_reconnect": 3,
                "kill_switch": 5, "drawdown_breaker": 5,
                "bot_startup": 2, "bot_shutdown": 2,
            },
        },
    }
    base["notifications"].update(overrides)
    return base


@pytest.mark.asyncio
async def test_notifier_send_success():
    n = Notifier(_notif_config())
    resp = MagicMock(); resp.status_code = 200
    n._session.post = MagicMock(return_value=resp)
    await n.send("title", "msg", priority=3, tags=["x"])
    n._session.post.assert_called_once()


@pytest.mark.asyncio
async def test_notifier_rate_limit_drops():
    n = Notifier(_notif_config(rate_limit_per_minute=1))
    resp = MagicMock(); resp.status_code = 200
    n._session.post = MagicMock(return_value=resp)
    await n.send("t", "m1")
    await n.send("t", "m2")  # over limit
    assert n._session.post.call_count == 1


@pytest.mark.asyncio
async def test_notifier_handles_http_error():
    n = Notifier(_notif_config())
    resp = MagicMock(); resp.status_code = 500
    n._session.post = MagicMock(return_value=resp)
    await n.send("t", "m")
    assert n._consecutive_failures == 1


@pytest.mark.asyncio
async def test_notifier_post_exception_falls_through():
    n = Notifier(_notif_config())
    n._session.post = MagicMock(side_effect=RuntimeError("boom"))
    await n.send("t", "m")  # must not raise


@pytest.mark.asyncio
async def test_notifier_lifecycle():
    n = Notifier(_notif_config())
    await n.start()
    await n.shutdown()


@pytest.mark.asyncio
async def test_notifier_convenience_methods_call_send():
    n = Notifier(_notif_config())
    resp = MagicMock(); resp.status_code = 200
    n._session.post = MagicMock(return_value=resp)
    await n.trade_entry("SPY", "BUY", 100.0, 10, "rsi_oversold")
    await n.position_closed("SPY", 5.0, "1h", "take_profit")
    await n.position_closed("SPY", -5.0, "1h", "stop_loss")  # negative branch
    await n.stop_loss_hit("SPY", -5.0)
    await n.daily_summary(10, 100.0, 0.6)
    await n.phase0_cleanup("plan text")
    await n.phase_transition(1, 2, 1500.0)  # upgrade
    await n.phase_transition(3, 1, 800.0)   # demotion
    await n.gateway_alert("disconnected", is_critical=True)
    await n.gateway_alert("reconnected", is_critical=False)
    await n.kill_switch("emergency")
    await n.drawdown_alert(0.05, 1)
    await n.bot_startup(phase=1, positions=0, mode="paper")
    await n.bot_shutdown(daily_pnl=10.0)
    # All triggered HTTP posts (some may have been rate-limited; just check >0)
    assert n._session.post.call_count > 0


def test_notifier_kill_topic_property():
    cfg = _notif_config()
    cfg["notifications"]["ntfy_kill_topic"] = "kill-topic"
    n = Notifier(cfg)
    assert n.kill_topic == "kill-topic"
    assert n.is_ntfy_available is True


def test_notifier_osa_safe_strips_dangerous_chars():
    cleaned = Notifier._osa_safe('Hello "world" with `bad` $stuff and \\backslash')
    assert '"' not in cleaned
    assert "\\" not in cleaned
    assert "`" not in cleaned
    assert "$" not in cleaned


def test_notifier_osa_safe_drops_non_ascii():
    cleaned = Notifier._osa_safe("hello ☃ world \U0001f600")
    assert "☃" not in cleaned
    assert "hello" in cleaned


@pytest.mark.asyncio
async def test_notifier_osascript_fallback_invoked():
    cfg = _notif_config(fallback_to_osascript=True)
    n = Notifier(cfg)
    n._session.post = MagicMock(side_effect=RuntimeError("offline"))
    with patch("trading_bot.notifications.notifier.subprocess.run") as run:
        await n.send("title", "msg")
        run.assert_called_once()


@pytest.mark.asyncio
async def test_notifier_osascript_fallback_swallows_error():
    cfg = _notif_config(fallback_to_osascript=True)
    n = Notifier(cfg)
    n._session.post = MagicMock(side_effect=RuntimeError("offline"))
    with patch("trading_bot.notifications.notifier.subprocess.run",
               side_effect=RuntimeError("no osascript")):
        await n.send("title", "msg")  # must not raise


# ===========================================================================
# HealthServer
# ===========================================================================


@pytest.fixture
def health_config(config, tmp_db_path):
    """Wrap the real Config but override db_path to our temp DB."""
    config._raw["db"] = {"path": tmp_db_path}
    return config


def _make_health(health_config) -> HealthServer:
    gw = MagicMock()
    gw.is_connected = True
    return HealthServer(health_config, gateway=gw)


@pytest.mark.asyncio
async def test_health_handler_returns_running(health_config):
    hs = _make_health(health_config)
    request = MagicMock()
    response = await hs._handle_health(request)
    assert response.status == 200
    body = json.loads(response.body)
    assert body["status"] == "running"
    assert body["gateway_connected"] is True
    assert "phase" in body
    assert "risk_status" in body


@pytest.mark.asyncio
async def test_health_handler_degraded_when_disconnected(health_config):
    hs = _make_health(health_config)
    hs._gateway.is_connected = False
    response = await hs._handle_health(MagicMock())
    body = json.loads(response.body)
    assert body["status"] == "degraded"


@pytest.mark.asyncio
async def test_health_handler_paused_status(health_config):
    hs = _make_health(health_config)
    hs.bot_status = "paused"
    response = await hs._handle_health(MagicMock())
    body = json.loads(response.body)
    assert body["status"] == "paused"


@pytest.mark.asyncio
async def test_health_handler_error_status(health_config):
    hs = _make_health(health_config)
    hs.bot_status = "error"
    response = await hs._handle_health(MagicMock())
    body = json.loads(response.body)
    assert body["status"] == "error"


@pytest.mark.asyncio
async def test_health_handler_swallows_db_errors(health_config, monkeypatch):
    hs = _make_health(health_config)
    # Point at a path that doesn't exist
    health_config._raw["db"] = {"path": "/nonexistent/path/db.sqlite"}
    response = await hs._handle_health(MagicMock())
    assert response.status == 200


def test_health_get_risk_status_with_risk_manager(health_config):
    rm = MagicMock()
    rm.is_paused = True
    hs = HealthServer(health_config, gateway=MagicMock(), risk_manager=rm)
    status = hs._get_risk_status(1000.0, -10.0, 5)
    assert status["is_paused"] is True
    assert "daily_loss_remaining_usd" in status
    assert "trades_remaining" in status


def test_health_get_risk_status_without_manager(health_config):
    hs = _make_health(health_config)
    status = hs._get_risk_status(1000.0, 0.0, 0)
    assert status["is_paused"] is False


@pytest.mark.asyncio
async def test_health_start_stop(health_config, monkeypatch):
    hs = _make_health(health_config)
    fake_runner = MagicMock()
    fake_runner.setup = MagicMock(return_value=__async_noop())
    fake_runner.cleanup = MagicMock(return_value=__async_noop())
    fake_site = MagicMock()
    fake_site.start = MagicMock(return_value=__async_noop())
    fake_site.stop = MagicMock(return_value=__async_noop())
    with patch("trading_bot.health.server.web.AppRunner", return_value=fake_runner), \
         patch("trading_bot.health.server.web.TCPSite", return_value=fake_site):
        await hs.start()
        await hs.stop()


async def __async_noop():
    return None


# ===========================================================================
# log_setup
# ===========================================================================


def test_setup_logging_creates_log_file(tmp_path, monkeypatch):
    # Redirect log directory to tmp_path
    monkeypatch.setattr("trading_bot.log_setup._LOG_DIR", tmp_path)
    log_path = setup_logging("test_logger", level=logging.DEBUG)
    assert log_path.exists()
    assert log_path.name == "test_logger.log"


def test_setup_logging_replaces_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr("trading_bot.log_setup._LOG_DIR", tmp_path)
    setup_logging("first")
    setup_logging("second")
    root = logging.getLogger()
    # Should have exactly 2 handlers (console + file)
    assert len(root.handlers) == 2


# ===========================================================================
# save_daily_summary — schema match (2026-04-29 incident)
# ===========================================================================
#
# The INSERT referenced ``lse_trades`` and ``commission_ratio`` but the
# schema didn't, so every save raised OperationalError, was swallowed,
# and the wind-down handler latched ``daily_summary_saved=true`` anyway.
# Result: empty table, blind to PnL.


def test_save_daily_summary_writes_row(tmp_db_path):
    from trading_bot.db import repository as repo

    summary = {
        "date": "2026-04-30",
        "total_trades": 3,
        "wins": 2,
        "losses": 1,
        "gross_pnl_usd": 12.5,
        "commissions_usd": 0.0,
        "net_pnl_usd": 12.5,
        "account_equity_usd": 100012.5,
        "max_drawdown_pct": 0.005,
        "win_rate": 0.667,
        "avg_win_usd": 7.0,
        "avg_loss_usd": -1.5,
        "profit_factor": 9.33,
        "phase": 1,
        "us_trades": 3,
        "notes": None,
    }

    conn = sqlite3.connect(tmp_db_path)
    try:
        repo.save_daily_summary(conn, summary)
        row = conn.execute(
            "SELECT date, total_trades, net_pnl_usd, phase, account_equity_usd "
            "FROM daily_summaries WHERE date = ?",
            ("2026-04-30",),
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "save_daily_summary did not persist row"
    assert row[0] == "2026-04-30"
    assert row[1] == 3
    assert abs(row[2] - 12.5) < 1e-6
    assert row[3] == 1
    assert abs(row[4] - 100012.5) < 1e-6


def test_save_daily_summary_replaces_existing_row(tmp_db_path):
    from trading_bot.db import repository as repo

    base = {
        "date": "2026-04-30",
        "total_trades": 1,
        "wins": 1,
        "losses": 0,
        "gross_pnl_usd": 5.0,
        "commissions_usd": 0.0,
        "net_pnl_usd": 5.0,
        "account_equity_usd": 100005.0,
        "phase": 1,
        "us_trades": 1,
    }
    conn = sqlite3.connect(tmp_db_path)
    try:
        repo.save_daily_summary(conn, base)
        # End-of-day re-save with updated metrics.
        updated = {**base, "total_trades": 5, "net_pnl_usd": 25.0}
        repo.save_daily_summary(conn, updated)
        row = conn.execute(
            "SELECT total_trades, net_pnl_usd FROM daily_summaries WHERE date = ?",
            ("2026-04-30",),
        ).fetchone()
    finally:
        conn.close()

    assert row == (5, 25.0)


def test_save_daily_summary_ignores_dropped_lse_fields(tmp_db_path):
    """Stray ``lse_trades``/``commission_ratio`` values must not crash."""
    from trading_bot.db import repository as repo

    summary = {
        "date": "2026-04-30",
        "account_equity_usd": 100000.0,
        "phase": 1,
        # Legacy keys that no longer exist in schema:
        "lse_trades": 0,
        "commission_ratio": 0.0,
    }
    conn = sqlite3.connect(tmp_db_path)
    try:
        repo.save_daily_summary(conn, summary)
        count = conn.execute(
            "SELECT COUNT(*) FROM daily_summaries"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1

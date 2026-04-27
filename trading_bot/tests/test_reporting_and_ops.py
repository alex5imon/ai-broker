"""Tests for Tier 4: performance, notifier, health/server, log_setup."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from trading_bot.constants import TZ_EASTERN
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
    pnl_gbp: float = 1.0,
    gross_pnl: float = 1.0,
    currency: str = "USD",
    fx_rate: float = 1.25,
    side: str = "BUY",
    quantity: int = 10,
) -> None:
    if exit_time is None:
        exit_time = datetime.now(tz=ET).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO trades (
                ticker, exchange, currency, side, entry_time, exit_time,
                entry_price, exit_price, quantity, hold_type, phase,
                signal_price, gross_pnl, pnl_gbp, fx_rate
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ticker, "US", currency, side,
                exit_time, exit_time, 100.0, 101.0, quantity, "intraday", 1,
                100.0, gross_pnl, pnl_gbp, fx_rate,
            ),
        )
        conn.commit()


def _seed_daily_summary(
    db_path: str, *, date_str: str, total_trades: int = 1, wins: int = 1,
    losses: int = 0, gross_pnl_gbp: float = 1.0, commissions_gbp: float = 0.0,
    net_pnl_gbp: float = 1.0, account_equity_gbp: float = 1000.0,
    phase: int = 1,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO daily_summaries (
                date, total_trades, wins, losses, gross_pnl_gbp,
                commissions_gbp, net_pnl_gbp, win_rate, account_equity_gbp, phase
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                date_str, total_trades, wins, losses,
                gross_pnl_gbp, commissions_gbp, net_pnl_gbp,
                wins / max(total_trades, 1), account_equity_gbp, phase,
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
    today_str = date.today().isoformat()
    today_iso = datetime.now(tz=ET).isoformat()
    # Two wins, one loss
    _seed_trade(tmp_db_path, ticker="A", exit_time=today_iso, pnl_gbp=10.0, gross_pnl=12.5)
    _seed_trade(tmp_db_path, ticker="B", exit_time=today_iso, pnl_gbp=5.0, gross_pnl=6.25)
    _seed_trade(tmp_db_path, ticker="C", exit_time=today_iso, pnl_gbp=-3.0, gross_pnl=-3.75)
    m = pc.calculate_daily_metrics(today_str)
    assert m["total_trades"] == 3
    assert m["wins"] == 2
    assert m["losses"] == 1
    assert m["win_rate"] == pytest.approx(2 / 3, rel=1e-3)
    assert m["profit_factor"] == pytest.approx(15.0 / 3.0, rel=1e-3)
    assert m["largest_win_gbp"] == 10.0
    assert m["largest_loss_gbp"] == -3.0


def test_daily_metrics_gbx_currency_conversion(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    today_str = date.today().isoformat()
    today_iso = datetime.now(tz=ET).isoformat()
    # GBX trade: gross_pnl=1000 pence → 10 GBP
    _seed_trade(
        tmp_db_path, ticker="X", exit_time=today_iso,
        pnl_gbp=10.0, gross_pnl=1000.0, currency="GBX",
    )
    m = pc.calculate_daily_metrics(today_str)
    assert m["gross_pnl_gbp"] == 10.0


def test_daily_metrics_gbp_passthrough(tmp_db_path):
    pc = PerformanceCalculator(tmp_db_path)
    today_str = date.today().isoformat()
    today_iso = datetime.now(tz=ET).isoformat()
    _seed_trade(
        tmp_db_path, ticker="X", exit_time=today_iso,
        pnl_gbp=10.0, gross_pnl=10.0, currency="GBP", fx_rate=1.0,
    )
    m = pc.calculate_daily_metrics(today_str)
    assert m["gross_pnl_gbp"] == 10.0


def test_intraday_drawdown():
    trades = [
        {"pnl_gbp": 10.0},   # cumulative 10, peak 10
        {"pnl_gbp": -5.0},   # cumulative 5, drawdown 5/10=50%
        {"pnl_gbp": -2.0},   # cumulative 3, drawdown 7/10=70%
        {"pnl_gbp": 100.0},  # cumulative 103, peak 103
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
        gross_pnl_gbp=12.0, net_pnl_gbp=11.50,
    )
    _seed_daily_summary(
        tmp_db_path, date_str="2026-04-02", total_trades=2, wins=1, losses=1,
        gross_pnl_gbp=5.0, net_pnl_gbp=4.50,
    )
    _seed_trade(tmp_db_path, ticker="A", exit_time="2026-04-01T10:00", pnl_gbp=10.0)
    _seed_trade(tmp_db_path, ticker="B", exit_time="2026-04-01T11:00", pnl_gbp=2.0)
    _seed_trade(tmp_db_path, ticker="C", exit_time="2026-04-01T12:00", pnl_gbp=-1.0)
    _seed_trade(tmp_db_path, ticker="D", exit_time="2026-04-02T10:00", pnl_gbp=5.0)
    _seed_trade(tmp_db_path, ticker="E", exit_time="2026-04-02T11:00", pnl_gbp=-1.0)

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
    _seed_daily_summary(tmp_db_path, date_str="2026-04-01", net_pnl_gbp=5.0)
    _seed_daily_summary(tmp_db_path, date_str="2026-04-02", net_pnl_gbp=3.0)
    curve = pc.get_equity_curve(n_days=10)
    assert curve[0]["date"] == "2026-04-01"
    assert curve[-1]["date"] == "2026-04-02"
    # Cumulative should sum
    assert curve[-1]["cumulative_pnl_gbp"] == pytest.approx(8.0)


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
    assert "daily_loss_remaining_gbp" in status
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

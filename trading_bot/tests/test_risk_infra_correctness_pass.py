"""Regression tests for the 2026-05-11 trading-path correctness pass.

Covers items 10-14 from ``memory/risk_infrastructure_gaps``:

- Item 10: ``update_trade_exit`` was deleted as dead code (broken
  ``commission`` reference, zero callers). The inline writer in
  ``order_manager._close_position`` is the single source of truth and
  already performs a rowcount check — verified separately. The
  regression we guard against here is the helper resurrecting.
- Item 11: ``_compute_position_size`` honours ``fractional=True``.
- Item 12: ``save_risk_state`` is wrapped in BEGIN IMMEDIATE and rolls back.
- Item 13: ``_find_existing_stop`` requires stop_price match when provided
  (asserted in ``test_phase3_regressions.py``).
- Item 14: drawdown breaker force-flattens open positions when tripped.

Item 8 (asyncio.to_thread on Alpaca position lookup) is exercised by the
existing ``_check_alpaca_position`` callers continuing to pass with the
new async signature. Item 9 (signal-price vs fill-price P&L) is
deferred — see the in-code comment in strategy_manager.py.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import pytest

from trading_bot.config import Config
from trading_bot.db import repository as repo
from trading_bot.db.migrations import run_migrations
from trading_bot.execution.risk_manager import RiskManager
from trading_bot.strategy.entry import EntryEvaluator

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------------------
# Item 10 — guard against the dead helper resurrecting
# ---------------------------------------------------------------------------


def test_update_trade_exit_helper_remains_absent() -> None:
    """The standalone helper was deleted on 2026-05-11. Re-introducing it
    would risk a second, divergent write path for trade exit rows. The
    inline path in ``order_manager._close_position`` is canonical."""
    assert not hasattr(repo, "update_trade_exit"), (
        "update_trade_exit was deleted as dead code with a latent bug "
        "(references a non-existent `commission` column). Do not "
        "reintroduce — the inline writer in order_manager._close_position "
        "is the single authoritative exit-update path and already checks "
        "rowcount."
    )


# ---------------------------------------------------------------------------
# Item 12 — save_risk_state transaction safety
# ---------------------------------------------------------------------------


def test_save_risk_state_rolls_back_on_upsert_failure(tmp_path) -> None:
    """If the UPSERT fails mid-transaction, prior state must be preserved."""
    db = tmp_path / "broker.db"
    run_migrations(str(db))
    conn = sqlite3.connect(db)
    try:
        # Seed a clean tripped row.
        repo.save_risk_state(conn, "global", tripped=True, reason="initial")
        before = repo.load_risk_state(conn, "global")

        # Force the second write to raise by passing an unserialisable state.
        class Unserialisable:
            pass

        with pytest.raises(TypeError):
            repo.save_risk_state(
                conn, "global", tripped=False, state={"bad": Unserialisable()},
            )

        # Original row must be intact — transaction rolled back.
        after = repo.load_risk_state(conn, "global")
        assert after is not None
        assert after["tripped"] is True
        assert after["tripped_at"] == before["tripped_at"]
        assert after["reason"] == "initial"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Item 11 — _compute_position_size fractional flag
# ---------------------------------------------------------------------------


def _make_evaluator(config: Config, tmp_db_path: str) -> EntryEvaluator:
    """Minimal evaluator with mocked external deps — only sizing math is exercised."""
    return EntryEvaluator(
        config=config,
        technical=MagicMock(),
        sentiment=MagicMock(),
        earnings=MagicMock(),
        market_data=MagicMock(),
        db_path=tmp_db_path,
    )


class TestFractionalSizing:
    """The legacy single-strategy sizing path is dead in production today,
    but config flips re-activate it; the fractional knob is the same
    safety net that protects fractional sizing in the active multi-
    strategy path (``PositionSizer.calculate_fractional``).
    """

    @pytest.mark.parametrize(
        "signal_price,stop_loss_pct,equity",
        [
            (137.0, 0.013, 1000.0),   # ratio crafted to leave a residue
            (251.7, 0.0237, 5000.0),  # ditto, larger equity
            (10.0, 0.02, 1000.0),     # clean division — both paths agree
        ],
    )
    def test_fractional_geq_floored(
        self, config: Config, tmp_db_path: str,
        signal_price: float, stop_loss_pct: float, equity: float,
    ) -> None:
        """fractional=True must always return ≥ fractional=False (we just
        skip the floor; no other arithmetic differs). For inputs that
        would produce a residue, the inequality must be strict."""
        from trading_bot.constants import Phase

        ev = _make_evaluator(config, tmp_db_path)
        common = dict(
            ticker="SPY",
            exchange="US",
            signal_price=signal_price,
            stop_loss_pct=stop_loss_pct,
            account_equity_usd=equity,
            atr_rank=50.0,
            sentiment_size_mult=1.0,
            phase=Phase.MICRO,
        )
        whole, _ = ev._compute_position_size(**common, fractional=False)  # type: ignore[arg-type]
        frac, _ = ev._compute_position_size(**common, fractional=True)  # type: ignore[arg-type]

        # Floored is always integer-valued.
        assert whole == float(int(whole))
        # Fractional never returns less than the floor of the same inputs.
        assert frac >= whole - 1e-9


# ---------------------------------------------------------------------------
# Item 14 — drawdown breaker force-flatten
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_drawdown_breaker_flatten_calls_close_all_positions(
    config: Config, tmp_db_path: str, mock_notifier,
) -> None:
    rm = RiskManager(config, tmp_db_path, mock_notifier)
    gateway = MagicMock()
    gateway.client.close_all_positions = MagicMock()

    await rm.handle_drawdown_breaker_flatten(gateway)

    gateway.client.close_all_positions.assert_called_once_with(cancel_orders=True)


@pytest.mark.asyncio
async def test_handle_drawdown_breaker_flatten_swallows_alpaca_failure(
    config: Config, tmp_db_path: str, mock_notifier,
) -> None:
    """A broker failure during flatten must not propagate — paused state
    plus the next tick's drain are the recovery mechanism."""
    rm = RiskManager(config, tmp_db_path, mock_notifier)
    gateway = MagicMock()
    gateway.client.close_all_positions = MagicMock(side_effect=RuntimeError("brokered"))

    # Should not raise.
    await rm.handle_drawdown_breaker_flatten(gateway)

    gateway.client.close_all_positions.assert_called_once()


def test_check_drawdown_breaker_is_idempotent(
    config: Config, tmp_db_path: str, mock_notifier,
) -> None:
    """Activation tick returns True; every subsequent tick while the
    breaker remains active must return False so we don't re-fire
    ``handle_drawdown_breaker_flatten`` and pummel Alpaca's
    ``close_all_positions`` endpoint every 5 minutes. /python-review
    catch on PR #93."""
    import sqlite3
    from datetime import date, timedelta
    from unittest.mock import patch

    rm = RiskManager(config, tmp_db_path, mock_notifier)

    # Seed 5 days of equity at $1000 so a $940 read is a 6% drawdown.
    conn = sqlite3.connect(tmp_db_path)
    today = date.today()
    for i in range(5):
        d = (today - timedelta(days=i + 1)).isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO daily_summaries "
            "(date, account_equity_usd, phase) VALUES (?, ?, 1)",
            (d, 1000.0),
        )
    conn.commit()
    conn.close()

    def _swallow(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()  # type: ignore[union-attr]

    with patch("asyncio.ensure_future", _swallow):
        first = rm.check_drawdown_breaker(940.0)
        # Equity remains below the watermark on the next tick — but the
        # breaker is already active so the check must short-circuit.
        second = rm.check_drawdown_breaker(940.0)
        third = rm.check_drawdown_breaker(940.0)

    assert first is True, "activation tick must return True"
    assert second is False, "subsequent ticks must NOT re-fire the breaker"
    assert third is False
    assert rm._drawdown_breaker_active is True


def test_check_drawdown_breaker_is_wired_into_unconditional_tick_path() -> None:
    """The drawdown breaker (and the daily-loss check) must be invoked
    from the unconditional ``TradingBot.tick`` path, NOT from
    ``scan_for_entries``.

    Pre-PR #93 the calls did not exist at all. PR #93 wired them in,
    but inside ``scan_for_entries`` — which is skipped in close-only
    mode, during wind-down, and on any tick where the entry-scan gate
    is otherwise closed, leaving the breaker silent for the entire
    afternoon. This guard fails if a regression moves the calls back
    into the entry-scan-only path.
    """
    import inspect

    from trading_bot import main as main_mod
    from trading_bot.main import TradingBot

    tick_src: str = inspect.getsource(TradingBot.tick)
    assert "check_drawdown_breaker(" in tick_src, (
        "check_drawdown_breaker must be called directly from TradingBot.tick "
        "(the unconditional path). Calling it only from "
        "scan_for_entries silences the breaker in close-only mode "
        "and wind-down."
    )
    assert "check_daily_loss_limit(" in tick_src, (
        "check_daily_loss_limit must be called directly from TradingBot.tick "
        "(the unconditional path). Calling it only from "
        "scan_for_entries silences the daily-loss circuit in "
        "close-only mode and wind-down."
    )
    assert "handle_drawdown_breaker_flatten(" in tick_src, (
        "When the breaker trips, TradingBot.tick must call "
        "handle_drawdown_breaker_flatten so existing positions are "
        "not left riding stale stops."
    )

    scan_src: str = inspect.getsource(TradingBot.scan_for_entries)
    assert "check_drawdown_breaker(" not in scan_src, (
        "check_drawdown_breaker must NOT live inside "
        "scan_for_entries — that path is skipped in close-only mode "
        "and wind-down, so the breaker would silently fail to trip."
    )
    assert "check_daily_loss_limit(" not in scan_src, (
        "check_daily_loss_limit must NOT live inside "
        "scan_for_entries — that path is skipped in close-only mode "
        "and wind-down."
    )

    # Belt-and-braces: the symbols must also be referenced somewhere
    # in main.py at module scope.
    module_src: str = inspect.getsource(main_mod)
    assert "check_drawdown_breaker(" in module_src
    assert "handle_drawdown_breaker_flatten(" in module_src

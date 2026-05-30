"""Short order-side tests (PR3 for gap-fill sleeve #48).

Issue #126 (PR #177) added the ``positions.side`` column, hydration, and a
``side``-based P&L-sign helper — but left the *order submission* sides
long-only: the protective stop, the cover, and the emergency flatten were
hardcoded SELL. A live short would therefore enter correctly (SELL) but get
a SELL protective stop, a SELL cover, and a SELL flatten — each of which
*adds to* the short rather than protecting/closing it.

This suite pins the order-side path: short positions get a BUY protective
stop (above entry), cover with a BUY (market + limit), cancel the protective
BUY leg (not the SELL entry), and emergency-flatten with a BUY. Long-path
guards confirm the SELL behaviour is unchanged.

Mocked Alpaca client only — real-broker (paper) validation of shorting
permissions / margin / stop acceptance is a gating prerequisite to
enablement (PR6), not covered here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from alpaca.trading.enums import OrderSide

from trading_bot.constants import PositionStatus
from trading_bot.execution.order_manager import (
    EntryDecision,
    OrderManager,
    _ActiveOrder,
)

pytestmark = pytest.mark.critical


def _make_om(config, db_path: str, notifier) -> OrderManager:
    gw = MagicMock()
    gw.client = MagicMock()
    return OrderManager(gw, config, notifier, db_path)


def _entry(*, side: str = "BUY", ticker: str = "SPY", price: float = 100.0) -> EntryDecision:
    is_short = side == "SELL"
    return EntryDecision(
        ticker=ticker, exchange="US", side=side, shares=10.0, limit_price=price,
        stop_price=price * (1.02 if is_short else 0.98),
        target_price=price * (0.97 if is_short else 1.04),
        hold_type="intraday", sector="Information Technology", phase=1,
        sentiment_score=0.0, signals="test", currency="USD", strategy_id="gap_fill",
    )


def _last_request(om: OrderManager) -> Any:
    return om._gw.client.submit_order.call_args.kwargs["order_data"]


def _seed_open_short(om: OrderManager) -> int:
    """Create a short position row + in-memory _ActiveOrder; return trade_id."""
    trade_id = om._create_position_record(_entry(side="SELL"))
    assert trade_id is not None
    om._active_orders[trade_id] = _ActiveOrder(
        trade_id=trade_id, ticker="SPY", exchange="US", side="SELL",
        status=PositionStatus.STOP_ACTIVE, entry_shares=10.0, filled_shares=10.0,
        entry_price=100.0, stop_price=102.0, target_price=97.0,
        db_trade_id=om._pending_db_trade_ids.get(trade_id),
    )
    return trade_id


# ---------------------------------------------------------------------------
# Protective stop side
# ---------------------------------------------------------------------------

class TestProtectiveStopSide:
    @pytest.mark.asyncio
    async def test_short_stop_is_buy(self, config, tmp_db_path, mock_notifier) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1, ticker="SPY", exchange="US", side="SELL",
            entry_price=100.0, stop_price=102.0,
        )
        stop_id = await om._place_standalone_stop(1, active, qty=10.0)
        assert stop_id is not None
        assert _last_request(om).side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_long_stop_is_sell(self, config, tmp_db_path, mock_notifier) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        active = _ActiveOrder(
            trade_id=1, ticker="SPY", exchange="US", side="BUY",
            entry_price=100.0, stop_price=98.0,
        )
        stop_id = await om._place_standalone_stop(1, active, qty=10.0)
        assert stop_id is not None
        assert _last_request(om).side == OrderSide.SELL


# ---------------------------------------------------------------------------
# Cover sides (market + limit) and cancel filter
# ---------------------------------------------------------------------------

class TestCoverSides:
    @pytest.mark.asyncio
    async def test_market_cover_of_short_buys_and_cancels_buy_stop(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _seed_open_short(om)
        om.cancel_all_for_ticker = AsyncMock()  # type: ignore[method-assign]
        await om.place_exit(ticker="SPY", qty=10.0, reason="take_profit")
        assert _last_request(om).side == OrderSide.BUY
        assert om.cancel_all_for_ticker.call_args.kwargs["side_filter"] == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_market_exit_of_long_sells_and_cancels_sell_stop(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        trade_id = om._create_position_record(_entry(side="BUY"))
        om._active_orders[trade_id] = _ActiveOrder(
            trade_id=trade_id, ticker="SPY", exchange="US", side="BUY",
            status=PositionStatus.STOP_ACTIVE, filled_shares=10.0, entry_price=100.0,
            db_trade_id=om._pending_db_trade_ids.get(trade_id),
        )
        om.cancel_all_for_ticker = AsyncMock()  # type: ignore[method-assign]
        await om.place_exit(ticker="SPY", qty=10.0, reason="take_profit")
        assert _last_request(om).side == OrderSide.SELL
        assert om.cancel_all_for_ticker.call_args.kwargs["side_filter"] == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_limit_cover_of_short_buys(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        _seed_open_short(om)
        om.cancel_all_for_ticker = AsyncMock()  # type: ignore[method-assign]
        await om.place_limit_exit(
            ticker="SPY", qty=10.0, limit_price=97.0, reason="take_profit",
        )
        assert _last_request(om).side == OrderSide.BUY
        assert om.cancel_all_for_ticker.call_args.kwargs["side_filter"] == OrderSide.BUY


# ---------------------------------------------------------------------------
# Emergency flatten side
# ---------------------------------------------------------------------------

class TestEmergencyFlattenSide:
    @pytest.mark.asyncio
    async def test_short_flatten_buys_to_cover(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        await om.emergency_flatten("SPY", 10.0, "US", position_side="SELL")
        assert _last_request(om).side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_long_flatten_sells(self, config, tmp_db_path, mock_notifier) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        await om.emergency_flatten("SPY", 10.0, "US")
        assert _last_request(om).side == OrderSide.SELL


# ---------------------------------------------------------------------------
# Stop-recovery side matching
# ---------------------------------------------------------------------------

def _mock_open_order(*, side: str, order_type: str = "stop", qty: float = 10.0,
                     order_id: str = "stop-1") -> MagicMock:
    o = MagicMock()
    o.id = order_id
    o.side = MagicMock()
    o.side.value = side
    o.order_type = MagicMock()
    o.order_type.value = order_type
    o.qty = qty
    o.stop_price = 102.0
    return o


class TestFindExistingStopSide:
    @pytest.mark.asyncio
    async def test_short_recovery_matches_buy_stop(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.get_orders = MagicMock(return_value=[
            _mock_open_order(side="sell"),            # the long stop — wrong side
            _mock_open_order(side="buy", order_id="buy-stop"),  # the short's stop
        ])
        found = await om._find_existing_stop(
            "SPY", 10.0, stop_price=102.0, position_side="SELL",
        )
        assert found == "buy-stop"

    @pytest.mark.asyncio
    async def test_long_recovery_matches_sell_stop(
        self, config, tmp_db_path, mock_notifier
    ) -> None:
        om = _make_om(config, tmp_db_path, mock_notifier)
        om._gw.client.get_orders = MagicMock(return_value=[
            _mock_open_order(side="buy", order_id="buy-stop"),
            _mock_open_order(side="sell", order_id="sell-stop"),
        ])
        found = await om._find_existing_stop("SPY", 10.0, stop_price=102.0)
        assert found == "sell-stop"

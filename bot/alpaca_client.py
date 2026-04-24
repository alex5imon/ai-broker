from __future__ import annotations

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from .config import Config


def build_client(cfg: Config) -> TradingClient:
    return TradingClient(cfg.api_key_id, cfg.api_secret, paper=cfg.is_paper)


def is_market_open(client: TradingClient) -> bool:
    return bool(client.get_clock().is_open)


def get_positions_by_symbol(client: TradingClient) -> dict[str, float]:
    return {p.symbol: float(p.qty) for p in client.get_all_positions()}


def submit_market_order(
    client: TradingClient,
    *,
    symbol: str,
    qty: float,
    side: OrderSide,
    client_order_id: str,
) -> None:
    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        client_order_id=client_order_id,
    )
    client.submit_order(order_data=req)

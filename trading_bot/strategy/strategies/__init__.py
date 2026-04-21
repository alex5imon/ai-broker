"""Concrete trading strategy implementations."""

from __future__ import annotations

from typing import Any

from trading_bot.strategy.base import StrategyBase
from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
from trading_bot.strategy.strategies.trend_following import TrendFollowingStrategy
from trading_bot.strategy.strategies.breakout import BreakoutStrategy
from trading_bot.strategy.strategies.sentiment_combo import SentimentComboStrategy

STRATEGY_REGISTRY: dict[str, type[StrategyBase]] = {
    "mean_reversion": MeanReversionStrategy,
    "trend_following": TrendFollowingStrategy,
    "breakout": BreakoutStrategy,
    "sentiment_combo": SentimentComboStrategy,
}


def create_strategies(strategy_configs: dict[str, dict[str, Any]]) -> list[StrategyBase]:
    """Instantiate all enabled strategies from config."""
    strategies: list[StrategyBase] = []
    for sid, cfg in strategy_configs.items():
        if not cfg.get("enabled", True):
            continue
        cls: type[StrategyBase] | None = STRATEGY_REGISTRY.get(sid)
        if cls is None:
            continue
        strategies.append(cls(config=cfg))
    return strategies

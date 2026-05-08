"""Concrete trading strategy implementations."""

from __future__ import annotations

from typing import Any

from trading_bot.strategy.base import StrategyBase
from trading_bot.strategy.strategies.mean_reversion import MeanReversionStrategy
from trading_bot.strategy.strategies.trend_following import TrendFollowingStrategy
from trading_bot.strategy.strategies.breakout import BreakoutStrategy
from trading_bot.strategy.strategies.sentiment_combo import SentimentComboStrategy
from trading_bot.strategy.strategies.overnight_drift import OvernightDriftStrategy
from trading_bot.strategy.strategies.opening_range_breakout import (
    OpeningRangeBreakoutStrategy,
)

STRATEGY_REGISTRY: dict[str, type[StrategyBase]] = {
    "mean_reversion": MeanReversionStrategy,
    "trend_following": TrendFollowingStrategy,
    "breakout": BreakoutStrategy,
    "sentiment_combo": SentimentComboStrategy,
    "overnight_drift": OvernightDriftStrategy,
    "opening_range_breakout": OpeningRangeBreakoutStrategy,
}


def create_strategies(
    strategy_configs: dict[str, dict[str, Any]],
    db_path: str | None = None,
    vol_target_config: dict[str, Any] | None = None,
) -> list[StrategyBase]:
    """Instantiate all enabled strategies from config.

    ``db_path`` and ``vol_target_config`` are optional so the backtester
    (which uses an in-memory closed-trades buffer) can keep its current
    constructor invocation. Live callers should pass both.
    """
    strategies: list[StrategyBase] = []
    for sid, cfg in strategy_configs.items():
        if not cfg.get("enabled", True):
            continue
        cls: type[StrategyBase] | None = STRATEGY_REGISTRY.get(sid)
        if cls is None:
            continue
        # Each concrete subclass overrides `__init__` to take
        # (config, db_path, vol_target_config) — narrower than the base
        # class signature mypy sees. The registry pattern is the only
        # caller, so the kwargs are guaranteed to match.
        strategies.append(
            cls(  # type: ignore[call-arg]
                config=cfg,
                db_path=db_path,
                vol_target_config=vol_target_config,
            )
        )
    return strategies

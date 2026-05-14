"""Legacy entry-signal module — superseded by ``StrategyManager``.

The ``EntryEvaluator`` orchestrator and its strategy-side ``EntryDecision``
dataclass were removed in ai-broker#125. The live tick now routes every
candidate through ``trading_bot.strategy.strategy_manager.StrategyManager``,
which composes per-sleeve ``StrategyBase`` instances. Order-manager
decisions are represented by ``trading_bot.execution.order_manager.EntryDecision``.

This module is intentionally left as a stub so that
``from trading_bot.strategy.entry import ...`` raises a clean
``ImportError`` rather than masquerading as a working path.
"""

from __future__ import annotations

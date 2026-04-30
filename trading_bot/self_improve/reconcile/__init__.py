"""Read-only reconciliation: local SQLite state vs live Alpaca account.

Run as a module to produce a markdown inventory report::

    python -m trading_bot.self_improve.reconcile [--db PATH] [--since YYYY-MM-DD]
                                                  [--output PATH]

The package never writes to the DB and never submits orders. It pulls every
order + position in the window, walks the local DB, classifies each row, and
renders a markdown report grouped by classification with a proposed corrective
action per row.

See ``trading_bot/docs/self_improve_followups.md`` for the data-layer bug hypotheses this
report is designed to confirm.
"""

from __future__ import annotations

from trading_bot.self_improve.reconcile.alpaca_fetch import (
    AlpacaFetcher,
    AlpacaOrderRec,
    AlpacaPosition,
    AlpacaState,
    fetch_alpaca_state,
)
from trading_bot.self_improve.reconcile.classify import (
    PositionClass,
    PositionFinding,
    TradeClass,
    TradeFinding,
    classify_position,
    classify_trade,
)
from trading_bot.self_improve.reconcile.db_loaders import (
    _position_lookup,
    load_db_positions,
    load_db_trades,
    load_strategy_enabled_map,
)
from trading_bot.self_improve.reconcile.report import (
    ReconcileReport,
    build_report,
    render_markdown,
)

__all__: list[str] = [
    "AlpacaFetcher",
    "AlpacaOrderRec",
    "AlpacaPosition",
    "AlpacaState",
    "PositionClass",
    "PositionFinding",
    "ReconcileReport",
    "TradeClass",
    "TradeFinding",
    "build_report",
    "classify_position",
    "classify_trade",
    "fetch_alpaca_state",
    "load_db_positions",
    "load_db_trades",
    "load_strategy_enabled_map",
    "render_markdown",
    "_position_lookup",
]

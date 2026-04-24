"""Virtual portfolio tracking for multi-strategy sub-allocations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


class VirtualPortfolio:
    """Tracks virtual cash allocation for one strategy."""

    def __init__(self, strategy_id: str, display_name: str, initial_cash: float, db_path: str) -> None:
        self.strategy_id: str = strategy_id
        self.display_name: str = display_name
        self._db_path: str = db_path
        self._ensure_row(initial_cash)

    def _ensure_row(self, initial_cash: float) -> None:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT strategy_id FROM strategy_portfolios WHERE strategy_id = ?",
                (self.strategy_id,),
            ).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO strategy_portfolios
                       (strategy_id, display_name, initial_cash, current_cash)
                       VALUES (?, ?, ?, ?)""",
                    (self.strategy_id, self.display_name, initial_cash, initial_cash),
                )
                conn.commit()
                logger.info("Created virtual portfolio for %s with $%.2f", self.strategy_id, initial_cash)
        finally:
            conn.close()

    @property
    def current_cash(self) -> float:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT current_cash FROM strategy_portfolios WHERE strategy_id = ?",
                (self.strategy_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    @property
    def available_cash(self) -> float:
        """Current cash minus unsettled amounts for this strategy."""
        cash: float = self.current_cash
        pending: float = self._get_pending_settlements()
        return max(cash - pending, 0.0)

    def record_entry(self, shares: int, price: float) -> None:
        cost: float = shares * price
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "UPDATE strategy_portfolios SET current_cash = current_cash - ?, updated_at = datetime('now') WHERE strategy_id = ?",
                (cost, self.strategy_id),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("[%s] Entry recorded: %d shares @ $%.2f ($%.2f deducted)", self.strategy_id, shares, price, cost)

    def record_exit(self, shares: int, exit_price: float, entry_price: float) -> None:
        proceeds: float = shares * exit_price
        pnl: float = shares * (exit_price - entry_price)
        is_win: bool = pnl > 0

        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """UPDATE strategy_portfolios SET
                   current_cash = current_cash + ?,
                   total_pnl = total_pnl + ?,
                   total_trades = total_trades + 1,
                   wins = wins + ?,
                   losses = losses + ?,
                   updated_at = datetime('now')
                   WHERE strategy_id = ?""",
                (proceeds, pnl, 1 if is_win else 0, 0 if is_win else 1, self.strategy_id),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info("[%s] Exit recorded: %d shares @ $%.2f, P&L=$%.2f", self.strategy_id, shares, exit_price, pnl)

    def get_open_positions(self) -> list[dict[str, Any]]:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM positions WHERE strategy_id = ? AND status != 'CLOSED'",
                (self.strategy_id,),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def get_stats(self) -> dict[str, Any]:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM strategy_portfolios WHERE strategy_id = ?",
                (self.strategy_id,),
            ).fetchone()
            if row is None:
                return {}
            d: dict[str, Any] = dict(row)
            total: int = d.get("total_trades", 0)
            wins: int = d.get("wins", 0)
            d["win_rate"] = wins / total if total > 0 else 0.0
            d["open_positions"] = len(self.get_open_positions())
            d["available_cash"] = self.available_cash
            return d
        finally:
            conn.close()

    def _get_pending_settlements(self) -> float:
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM settlements WHERE strategy_id = ? AND settled = 0",
                (self.strategy_id,),
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()


class PortfolioManager:
    """Manages all virtual portfolios across strategies."""

    def __init__(
        self,
        strategy_configs: dict[str, dict[str, Any]],
        total_cash: float,
        db_path: str,
    ) -> None:
        self._db_path: str = db_path
        self._portfolios: dict[str, VirtualPortfolio] = {}

        enabled_strategies: list[tuple[str, dict[str, Any]]] = [
            (sid, cfg) for sid, cfg in strategy_configs.items()
            if cfg.get("enabled", True)
        ]

        for sid, cfg in enabled_strategies:
            allocation: float = float(cfg.get("allocation_usd", total_cash / len(enabled_strategies)))
            display_name: str = sid.replace("_", " ").title()
            self._portfolios[sid] = VirtualPortfolio(sid, display_name, allocation, db_path)

        logger.info(
            "Portfolio manager initialized with %d strategies, total $%.2f",
            len(self._portfolios), total_cash,
        )

    def get_portfolio(self, strategy_id: str) -> VirtualPortfolio | None:
        return self._portfolios.get(strategy_id)

    def get_all_portfolios(self) -> dict[str, VirtualPortfolio]:
        return dict(self._portfolios)

    def get_global_position_count(self) -> int:
        return sum(len(p.get_open_positions()) for p in self._portfolios.values())

    def get_comparison_report(self) -> dict[str, dict[str, Any]]:
        return {sid: p.get_stats() for sid, p in self._portfolios.items()}

"""T+1 settlement tracking for a cash account.

Cash accounts enforce T+1 settlement for equity trades.  After selling a
position the proceeds are not available for new trades until the next
business day.  This module tracks pending settlements and computes
available settled cash in GBP.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any

logger: logging.Logger = logging.getLogger(__name__)


class SettlementTracker:
    """Tracks T+1 settlement for an IB cash account.

    All public amounts are in GBP (the account base currency) unless stated
    otherwise.  Individual settlement records store amounts in both the
    trade currency and GBP for auditability.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: dict[str, Any], db_path: str) -> None:
        self._db_path: str = db_path

        settlement_cfg: dict[str, Any] = config.get("settlement", {})
        self._t_plus_days: int = int(settlement_cfg.get("t_plus_days", 1))

        # Holiday lists from config (already ISO strings)
        holidays_cfg: dict[str, Any] = config.get("holidays", {})
        self._holidays: set[date] = set()
        for key, dates in holidays_cfg.items():
            # Skip early-close entries - they are still trading days
            if "early_close" in key:
                continue
            for d in dates:
                try:
                    self._holidays.add(date.fromisoformat(str(d)))
                except ValueError:
                    logger.warning("Invalid holiday date: %s", d)

        # Account base currency for cash queries
        acct_cfg: dict[str, Any] = config.get("account", {})
        self._base_currency: str = str(acct_cfg.get("base_currency", "GBP"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_sale(
        self,
        trade_id: int | None,
        ticker: str,
        amount: float,
        currency: str,
        fx_rate: float,
        sell_date: date,
    ) -> date:
        """Record a sale and return the expected settlement date.

        Parameters
        ----------
        trade_id:
            FK to ``trades.id``.  May be ``None`` for Phase 0 cleanup sells.
        ticker:
            Symbol that was sold.
        amount:
            Gross proceeds in the trade currency.
        currency:
            Trade currency (``GBP`` or ``USD``).
        fx_rate:
            GBP/USD rate at the time of the sale.  Use 1.0 for GBP trades.
        sell_date:
            Calendar date when the sell was executed.

        Returns
        -------
        date
            The expected settlement date (T+1 business day).
        """
        settle_date: date = self._next_business_day(sell_date, self._t_plus_days)

        # Convert to GBP
        if currency.upper() == "GBP":
            amount_gbp: float = amount
        else:
            # fx_rate is GBP per 1 USD  => amount_gbp = amount / fx_rate
            # But the convention in SPEC is GBP/USD rate, so 1 GBP = fx_rate USD
            # Therefore amount_gbp = amount / fx_rate
            amount_gbp = amount / fx_rate if fx_rate > 0 else amount

        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                """INSERT INTO settlements
                   (trade_id, ticker, amount, currency, amount_gbp,
                    sell_date, settle_date, settled)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    trade_id,
                    ticker,
                    amount,
                    currency.upper(),
                    round(amount_gbp, 2),
                    sell_date.isoformat(),
                    settle_date.isoformat(),
                ),
            )
            conn.commit()
            logger.info(
                "Recorded sale: %s %.2f %s (%.2f GBP) sell=%s settle=%s",
                ticker,
                amount,
                currency,
                amount_gbp,
                sell_date.isoformat(),
                settle_date.isoformat(),
            )
        finally:
            conn.close()

        return settle_date

    def get_available_cash_gbp(self) -> float:
        """Get settled cash available for trading, in GBP.

        ``available = total_cash_gbp - sum(unsettled amounts in GBP)``

        The total cash is read from the ``daily_summaries`` table (latest
        account_equity_gbp) or can be provided externally.  This method
        only computes the *deduction* from unsettled amounts and returns
        the total unsettled amount as a negative offset that the caller
        should subtract from the current cash balance.

        In practice the caller will:
            settled = account_cash_gbp - tracker.get_pending_total_gbp()
        """
        return self.get_pending_total_gbp()

    def get_pending_total_gbp(self) -> float:
        """Sum of all unsettled amounts in GBP.

        The caller subtracts this from the IB-reported cash balance to get
        the truly settled funds available for new entries.
        """
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount_gbp), 0) "
                "FROM settlements WHERE settled = 0"
            ).fetchone()
            total: float = float(row[0]) if row else 0.0
            return total
        except sqlite3.OperationalError:
            logger.warning("settlements table not found")
            return 0.0
        finally:
            conn.close()

    def get_pending_settlements(self) -> list[dict[str, Any]]:
        """Get all pending (unsettled) settlement records."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                "SELECT * FROM settlements WHERE settled = 0 "
                "ORDER BY settle_date ASC"
            )
            rows: list[dict[str, Any]] = [dict(r) for r in cursor.fetchall()]
            return rows
        except sqlite3.OperationalError:
            logger.warning("settlements table not found")
            return []
        finally:
            conn.close()

    def update_settlements(self) -> int:
        """Mark settlements as complete if ``settle_date <= today``.

        Returns the number of settlements marked as settled.
        """
        today_str: str = date.today().isoformat()
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        try:
            cursor = conn.execute(
                "UPDATE settlements SET settled = 1 "
                "WHERE settled = 0 AND settle_date <= ?",
                (today_str,),
            )
            count: int = cursor.rowcount
            conn.commit()
            if count:
                logger.info("Marked %d settlement(s) as settled", count)
            return count
        except sqlite3.OperationalError:
            logger.warning("settlements table not found - skipping update")
            return 0
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Business day calculation
    # ------------------------------------------------------------------

    def _next_business_day(self, from_date: date, t_plus: int = 1) -> date:
        """Calculate T+N business day from *from_date*.

        Skips weekends (Saturday=5, Sunday=6) and holidays loaded from
        config.  ``t_plus`` defaults to 1 for T+1 equity settlement.
        """
        current: date = from_date
        days_counted: int = 0

        while days_counted < t_plus:
            current += timedelta(days=1)
            # Skip weekends
            if current.weekday() >= 5:
                continue
            # Skip holidays
            if current in self._holidays:
                continue
            days_counted += 1

        return current

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_business_day(self, check_date: date) -> bool:
        """Return True if *check_date* is a business day (not weekend/holiday)."""
        if check_date.weekday() >= 5:
            return False
        return check_date not in self._holidays

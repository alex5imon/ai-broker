"""Performance metrics calculation from trade and summary data.

All monetary values are in GBP (the account base currency) unless
explicitly suffixed otherwise.  Dates are YYYY-MM-DD strings in
US/Eastern.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.constants import TZ_EASTERN, Phase

logger: logging.Logger = logging.getLogger(__name__)


class PerformanceCalculator:
    """Calculates trading performance metrics from the SQLite database."""

    def __init__(self, db_path: str) -> None:
        self._db_path: str = db_path

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a read-only connection with ``row_factory`` set."""
        conn: sqlite3.Connection = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Daily metrics
    # ------------------------------------------------------------------

    def calculate_daily_metrics(self, date: str) -> dict[str, Any]:
        """Calculate all performance metrics for a single trading day.

        Parameters
        ----------
        date:
            ``YYYY-MM-DD`` in US/Eastern.

        Returns
        -------
        dict with keys: total_trades, wins, losses, gross_pnl_gbp,
        commissions_gbp, net_pnl_gbp, win_rate, avg_win, avg_loss,
        profit_factor, max_drawdown_pct, lse_trades, us_trades,
        commission_ratio, largest_win_gbp, largest_loss_gbp, expectancy,
        trades (list of per-trade dicts).
        """
        conn: sqlite3.Connection = self._connect()
        try:
            trades_rows = conn.execute(
                """
                SELECT * FROM trades
                WHERE date(exit_time) = ?
                  AND exit_time IS NOT NULL
                ORDER BY exit_time
                """,
                (date,),
            ).fetchall()

            trades: list[dict[str, Any]] = self._rows_to_dicts(trades_rows)

            total_trades: int = len(trades)
            wins: int = 0
            losses: int = 0
            gross_pnl_gbp: float = 0.0
            commissions_gbp: float = 0.0
            win_amounts: list[float] = []
            loss_amounts: list[float] = []
            us_trades: int = 0

            for t in trades:
                pnl: float = t.get("pnl_gbp") or 0.0
                commission: float = t.get("commission") or 0.0
                fx: float = t.get("fx_rate") or 1.0
                currency: str = t.get("currency", "GBP")

                # gross_pnl in trade currency -> convert to GBP
                gross_trade: float = t.get("gross_pnl") or 0.0
                if currency == "USD":
                    gross_gbp: float = gross_trade / fx if fx else gross_trade
                    comm_gbp: float = commission / fx if fx else commission
                elif currency == "GBX":
                    gross_gbp = gross_trade / 100.0
                    comm_gbp = commission / 100.0
                else:
                    gross_gbp = gross_trade
                    comm_gbp = commission

                gross_pnl_gbp += gross_gbp
                commissions_gbp += comm_gbp

                if pnl > 0:
                    wins += 1
                    win_amounts.append(pnl)
                elif pnl < 0:
                    losses += 1
                    loss_amounts.append(pnl)

                us_trades += 1

            net_pnl_gbp: float = gross_pnl_gbp - commissions_gbp
            win_rate: float = (wins / total_trades) if total_trades > 0 else 0.0
            avg_win: float = (sum(win_amounts) / len(win_amounts)) if win_amounts else 0.0
            avg_loss: float = (sum(loss_amounts) / len(loss_amounts)) if loss_amounts else 0.0
            largest_win: float = max(win_amounts) if win_amounts else 0.0
            largest_loss: float = min(loss_amounts) if loss_amounts else 0.0

            sum_wins: float = sum(win_amounts)
            sum_losses_abs: float = abs(sum(loss_amounts))
            profit_factor: float = (
                (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")
            )
            if sum_wins == 0.0 and sum_losses_abs == 0.0:
                profit_factor = 0.0

            expectancy: float = (
                (win_rate * avg_win) - ((1.0 - win_rate) * abs(avg_loss))
            )

            commission_ratio: float = (
                (commissions_gbp / gross_pnl_gbp)
                if gross_pnl_gbp > 0
                else 0.0
            )

            # Max drawdown (intraday, from cumulative P&L curve)
            max_drawdown_pct: float = self._calculate_intraday_drawdown(trades)

            return {
                "date": date,
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "gross_pnl_gbp": round(gross_pnl_gbp, 2),
                "commissions_gbp": round(commissions_gbp, 2),
                "net_pnl_gbp": round(net_pnl_gbp, 2),
                "win_rate": round(win_rate, 4),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
                "max_drawdown_pct": round(max_drawdown_pct, 4),
                "us_trades": us_trades,
                "commission_ratio": round(commission_ratio, 4),
                "largest_win_gbp": round(largest_win, 2),
                "largest_loss_gbp": round(largest_loss, 2),
                "expectancy": round(expectancy, 2),
                "trades": trades,
            }
        finally:
            conn.close()

    @staticmethod
    def _calculate_intraday_drawdown(trades: list[dict[str, Any]]) -> float:
        """Compute max drawdown % from the intraday cumulative P&L curve."""
        if not trades:
            return 0.0

        cumulative: float = 0.0
        peak: float = 0.0
        max_dd: float = 0.0

        for t in trades:
            pnl: float = t.get("pnl_gbp") or 0.0
            cumulative += pnl
            if cumulative > peak:
                peak = cumulative
            drawdown: float = peak - cumulative
            if peak > 0 and drawdown / peak > max_dd:
                max_dd = drawdown / peak

        return max_dd

    # ------------------------------------------------------------------
    # Period metrics
    # ------------------------------------------------------------------

    def calculate_period_metrics(
        self, start_date: str, end_date: str
    ) -> dict[str, Any]:
        """Calculate aggregate metrics over a date range.

        Parameters
        ----------
        start_date, end_date:
            ``YYYY-MM-DD`` inclusive bounds.

        Returns
        -------
        dict with keys: trading_days, total_trades, wins, losses,
        gross_pnl_gbp, commissions_gbp, net_pnl_gbp, win_rate,
        avg_win, avg_loss, profit_factor, best_day, worst_day,
        daily_summaries.
        """
        conn: sqlite3.Connection = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM daily_summaries
                WHERE date BETWEEN ? AND ?
                ORDER BY date
                """,
                (start_date, end_date),
            ).fetchall()

            summaries: list[dict[str, Any]] = self._rows_to_dicts(rows)
            if not summaries:
                return self._empty_period_metrics(start_date, end_date)

            trading_days: int = len(summaries)
            total_trades: int = sum(s.get("total_trades", 0) for s in summaries)
            wins: int = sum(s.get("wins", 0) for s in summaries)
            losses: int = sum(s.get("losses", 0) for s in summaries)
            gross_pnl_gbp: float = sum(s.get("gross_pnl_gbp", 0.0) for s in summaries)
            commissions_gbp: float = sum(s.get("commissions_gbp", 0.0) for s in summaries)
            net_pnl_gbp: float = sum(s.get("net_pnl_gbp", 0.0) for s in summaries)

            win_rate: float = (wins / total_trades) if total_trades > 0 else 0.0

            # Best / worst day by net P&L
            best_day: dict[str, Any] = max(summaries, key=lambda s: s.get("net_pnl_gbp", 0.0))
            worst_day: dict[str, Any] = min(summaries, key=lambda s: s.get("net_pnl_gbp", 0.0))

            # Avg win / avg loss from per-trade data over the range
            trade_rows = conn.execute(
                """
                SELECT pnl_gbp FROM trades
                WHERE date(exit_time) BETWEEN ? AND ?
                  AND pnl_gbp IS NOT NULL
                """,
                (start_date, end_date),
            ).fetchall()

            win_amounts: list[float] = [r["pnl_gbp"] for r in trade_rows if r["pnl_gbp"] > 0]
            loss_amounts: list[float] = [r["pnl_gbp"] for r in trade_rows if r["pnl_gbp"] < 0]

            avg_win: float = (sum(win_amounts) / len(win_amounts)) if win_amounts else 0.0
            avg_loss: float = (sum(loss_amounts) / len(loss_amounts)) if loss_amounts else 0.0

            sum_wins: float = sum(win_amounts)
            sum_losses_abs: float = abs(sum(loss_amounts))
            profit_factor: float = (
                (sum_wins / sum_losses_abs) if sum_losses_abs > 0 else float("inf")
            )
            if sum_wins == 0.0 and sum_losses_abs == 0.0:
                profit_factor = 0.0

            commission_ratio: float = (
                (commissions_gbp / gross_pnl_gbp) if gross_pnl_gbp > 0 else 0.0
            )

            return {
                "start_date": start_date,
                "end_date": end_date,
                "trading_days": trading_days,
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "gross_pnl_gbp": round(gross_pnl_gbp, 2),
                "commissions_gbp": round(commissions_gbp, 2),
                "net_pnl_gbp": round(net_pnl_gbp, 2),
                "win_rate": round(win_rate, 4),
                "avg_win": round(avg_win, 2),
                "avg_loss": round(avg_loss, 2),
                "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else 999.99,
                "commission_ratio": round(commission_ratio, 4),
                "best_day": {"date": best_day["date"], "net_pnl_gbp": best_day.get("net_pnl_gbp", 0.0)},
                "worst_day": {"date": worst_day["date"], "net_pnl_gbp": worst_day.get("net_pnl_gbp", 0.0)},
                "daily_summaries": summaries,
            }
        finally:
            conn.close()

    @staticmethod
    def _empty_period_metrics(start_date: str, end_date: str) -> dict[str, Any]:
        return {
            "start_date": start_date,
            "end_date": end_date,
            "trading_days": 0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "gross_pnl_gbp": 0.0,
            "commissions_gbp": 0.0,
            "net_pnl_gbp": 0.0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "commission_ratio": 0.0,
            "best_day": {"date": start_date, "net_pnl_gbp": 0.0},
            "worst_day": {"date": start_date, "net_pnl_gbp": 0.0},
            "daily_summaries": [],
        }

    # ------------------------------------------------------------------
    # Sharpe ratio
    # ------------------------------------------------------------------

    def calculate_sharpe_ratio(
        self,
        daily_returns: list[float],
        risk_free_rate: float = 0.0,
    ) -> float:
        """Compute annualised Sharpe ratio from daily returns.

        Parameters
        ----------
        daily_returns:
            List of daily percentage returns (e.g. 0.003 for +0.3%).
        risk_free_rate:
            Annualised risk-free rate (e.g. 0.05 for 5%).

        Returns
        -------
        Annualised Sharpe ratio.  Returns 0.0 if fewer than 2 data points
        or zero standard deviation.
        """
        if len(daily_returns) < 2:
            return 0.0

        n: int = len(daily_returns)
        daily_rf: float = risk_free_rate / 252.0
        excess: list[float] = [r - daily_rf for r in daily_returns]

        mean_excess: float = sum(excess) / n
        variance: float = sum((x - mean_excess) ** 2 for x in excess) / (n - 1)
        std_dev: float = math.sqrt(variance)

        if std_dev == 0.0:
            return 0.0

        return (mean_excess / std_dev) * math.sqrt(252.0)

    # ------------------------------------------------------------------
    # Equity curve
    # ------------------------------------------------------------------

    def get_equity_curve(self, n_days: int = 30) -> list[dict[str, Any]]:
        """Return equity curve data for the last *n_days* trading days.

        Returns
        -------
        List of dicts with keys: date, account_equity_gbp, net_pnl_gbp,
        cumulative_pnl_gbp.  Ordered oldest to newest.
        """
        conn: sqlite3.Connection = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT date, account_equity_gbp, net_pnl_gbp
                FROM daily_summaries
                ORDER BY date DESC
                LIMIT ?
                """,
                (n_days,),
            ).fetchall()

            if not rows:
                return []

            # Reverse to chronological order
            summaries: list[dict[str, Any]] = self._rows_to_dicts(rows)[::-1]

            cumulative: float = 0.0
            curve: list[dict[str, Any]] = []
            for s in summaries:
                daily_pnl: float = s.get("net_pnl_gbp") or 0.0
                cumulative += daily_pnl
                curve.append({
                    "date": s["date"],
                    "account_equity_gbp": s.get("account_equity_gbp", 0.0),
                    "net_pnl_gbp": daily_pnl,
                    "cumulative_pnl_gbp": round(cumulative, 2),
                })

            return curve
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Per-ticker stats
    # ------------------------------------------------------------------

    def get_per_ticker_stats(self, date: str | None = None) -> list[dict[str, Any]]:
        """Per-ticker P&L breakdown.

        Parameters
        ----------
        date:
            If provided, filter to trades closed on this date.
            If ``None``, return all-time stats.

        Returns
        -------
        List of dicts with keys: ticker, exchange, trade_count, wins,
        losses, net_pnl_gbp, avg_pnl_gbp, win_rate.
        """
        conn: sqlite3.Connection = self._connect()
        try:
            if date:
                rows = conn.execute(
                    """
                    SELECT ticker, exchange,
                           COUNT(*) AS trade_count,
                           SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN pnl_gbp < 0 THEN 1 ELSE 0 END) AS losses,
                           COALESCE(SUM(pnl_gbp), 0.0) AS net_pnl_gbp,
                           COALESCE(AVG(pnl_gbp), 0.0) AS avg_pnl_gbp
                    FROM trades
                    WHERE date(exit_time) = ? AND pnl_gbp IS NOT NULL
                    GROUP BY ticker
                    ORDER BY net_pnl_gbp DESC
                    """,
                    (date,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT ticker, exchange,
                           COUNT(*) AS trade_count,
                           SUM(CASE WHEN pnl_gbp > 0 THEN 1 ELSE 0 END) AS wins,
                           SUM(CASE WHEN pnl_gbp < 0 THEN 1 ELSE 0 END) AS losses,
                           COALESCE(SUM(pnl_gbp), 0.0) AS net_pnl_gbp,
                           COALESCE(AVG(pnl_gbp), 0.0) AS avg_pnl_gbp
                    FROM trades
                    WHERE pnl_gbp IS NOT NULL
                    GROUP BY ticker
                    ORDER BY net_pnl_gbp DESC
                    """
                ).fetchall()

            results: list[dict[str, Any]] = []
            for r in rows:
                d: dict[str, Any] = dict(r)
                tc: int = d.get("trade_count", 0)
                w: int = d.get("wins", 0)
                d["win_rate"] = round(w / tc, 4) if tc > 0 else 0.0
                d["net_pnl_gbp"] = round(d.get("net_pnl_gbp", 0.0), 2)
                d["avg_pnl_gbp"] = round(d.get("avg_pnl_gbp", 0.0), 2)
                results.append(d)

            return results
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Phase progress
    # ------------------------------------------------------------------

    def get_phase_progress(
        self, current_equity: float, current_phase: int
    ) -> dict[str, Any]:
        """Calculate progress toward the next phase transition.

        Parameters
        ----------
        current_equity:
            Current account equity in GBP.
        current_phase:
            Current phase number (0-3).

        Returns
        -------
        dict with keys: current_phase, next_phase, equity_target,
        equity_progress_pct, trading_days, trading_days_target,
        estimated_days_remaining.
        """
        conn: sqlite3.Connection = self._connect()
        try:
            # Phase transition thresholds
            thresholds: dict[int, dict[str, Any]] = {
                0: {"equity": 0.0, "days": 0, "next": 1},
                1: {"equity": 5000.0, "days": 40, "next": 2},
                2: {"equity": 20000.0, "days": 60, "next": 3},
                3: {"equity": float("inf"), "days": 0, "next": 3},
            }

            phase_info: dict[str, Any] = thresholds.get(
                current_phase,
                {"equity": float("inf"), "days": 0, "next": current_phase},
            )
            equity_target: float = phase_info["equity"]
            days_target: int = phase_info["days"]
            next_phase: int = phase_info["next"]

            # Count trading days in current phase
            phase_rows = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM daily_summaries
                WHERE phase = ?
                """,
                (current_phase,),
            ).fetchone()
            trading_days: int = int(phase_rows["cnt"]) if phase_rows else 0

            # Equity progress
            equity_progress_pct: float = 0.0
            if equity_target < float("inf") and equity_target > 0:
                equity_progress_pct = min(
                    (current_equity / equity_target) * 100.0, 100.0
                )

            # Estimate days remaining based on average daily P&L
            recent_rows = conn.execute(
                """
                SELECT net_pnl_gbp FROM daily_summaries
                WHERE phase = ?
                ORDER BY date DESC LIMIT 30
                """,
                (current_phase,),
            ).fetchall()

            daily_returns: list[float] = [
                r["net_pnl_gbp"] for r in recent_rows if r["net_pnl_gbp"] is not None
            ]
            avg_daily_pnl: float = (
                (sum(daily_returns) / len(daily_returns)) if daily_returns else 0.0
            )

            estimated_days: int | None = None
            equity_remaining: float = equity_target - current_equity
            if avg_daily_pnl > 0 and equity_remaining > 0:
                estimated_days = int(math.ceil(equity_remaining / avg_daily_pnl))

            # Win rate from recent trades for display
            win_rate_rows = conn.execute(
                """
                SELECT pnl_gbp FROM trades
                WHERE pnl_gbp IS NOT NULL AND phase = ?
                ORDER BY exit_time DESC LIMIT 20
                """,
                (current_phase,),
            ).fetchall()
            recent_wins: int = sum(1 for r in win_rate_rows if r["pnl_gbp"] > 0)
            recent_total: int = len(win_rate_rows)
            recent_win_rate: float = (
                (recent_wins / recent_total) if recent_total > 0 else 0.0
            )

            return {
                "current_phase": current_phase,
                "next_phase": next_phase,
                "equity_target": equity_target if equity_target < float("inf") else None,
                "equity_current": round(current_equity, 2),
                "equity_progress_pct": round(equity_progress_pct, 1),
                "trading_days": trading_days,
                "trading_days_target": days_target,
                "trading_days_progress_pct": round(
                    min((trading_days / days_target) * 100.0, 100.0), 1
                ) if days_target > 0 else 100.0,
                "avg_daily_pnl_gbp": round(avg_daily_pnl, 2),
                "estimated_days_remaining": estimated_days,
                "recent_win_rate": round(recent_win_rate, 4),
                "at_max_phase": current_phase >= 3,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Rolling returns for Sharpe calculation
    # ------------------------------------------------------------------

    def get_daily_returns(self, n_days: int = 60) -> list[float]:
        """Return daily net P&L as fraction of equity for last *n_days*.

        Used for Sharpe ratio calculation.
        """
        conn: sqlite3.Connection = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT net_pnl_gbp, account_equity_gbp
                FROM daily_summaries
                WHERE account_equity_gbp > 0
                ORDER BY date DESC
                LIMIT ?
                """,
                (n_days,),
            ).fetchall()

            returns: list[float] = []
            for r in rows:
                equity: float = r["account_equity_gbp"]
                pnl: float = r["net_pnl_gbp"] or 0.0
                if equity > 0:
                    returns.append(pnl / equity)

            # Reverse to chronological order
            returns.reverse()
            return returns
        finally:
            conn.close()

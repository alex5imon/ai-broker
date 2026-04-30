"""HTML report generation via Jinja2 templates.

Generates daily, weekly, and monthly reports from trade data and
performance metrics.  Reports are saved to the configured output
directory as standalone HTML files with no external dependencies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, TemplateNotFound

from trading_bot.config import Config
from trading_bot.constants import TZ_EASTERN
from trading_bot.reporting.performance import PerformanceCalculator

logger: logging.Logger = logging.getLogger(__name__)


class ReportGenerator:
    """Generates HTML reports from trading data.

    Reports are rendered from Jinja2 templates stored in the configured
    ``templates_dir`` and written to ``output_dir``.  Both paths are
    resolved from config.yaml.
    """

    def __init__(
        self,
        config: Config,
        performance: PerformanceCalculator,
    ) -> None:
        self._config: Config = config
        self._performance: PerformanceCalculator = performance

        templates_dir: str = str(
            config._get("reporting", "templates_dir")
            or "trading_bot/reporting/templates"
        )
        self._templates_path: Path = Path(templates_dir).resolve()
        self._output_dir: Path = Path(config.report_output_dir).expanduser().resolve()

        self._env: Environment = Environment(
            loader=FileSystemLoader(str(self._templates_path)),
            autoescape=True,
        )
        # Register custom filters
        self._env.filters["fmt_pnl"] = _fmt_pnl
        self._env.filters["fmt_pct"] = _fmt_pct
        self._env.filters["fmt_money"] = _fmt_money
        self._env.filters["fmt_time"] = _fmt_time

        logger.info(
            "ReportGenerator initialised: templates=%s output=%s",
            self._templates_path,
            self._output_dir,
        )

    # ------------------------------------------------------------------
    # Output helpers
    # ------------------------------------------------------------------

    def _ensure_output_dir(self) -> None:
        """Create output directory if it does not exist."""
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def _write_report(self, filename: str, html: str) -> str:
        """Write *html* to *filename* in the output directory, returning full path."""
        self._ensure_output_dir()
        filepath: Path = self._output_dir / filename
        filepath.write_text(html, encoding="utf-8")
        logger.info("Report written: %s", filepath)
        return str(filepath)

    # ------------------------------------------------------------------
    # Daily report
    # ------------------------------------------------------------------

    def generate_daily_report(self, report_date: str) -> str:
        """Generate a daily HTML report for *report_date* (YYYY-MM-DD).

        Returns the absolute file path of the generated report.
        """
        metrics: dict[str, Any] = self._performance.calculate_daily_metrics(report_date)
        ticker_stats: list[dict[str, Any]] = self._performance.get_per_ticker_stats(report_date)
        equity_curve: list[dict[str, Any]] = self._performance.get_equity_curve(
            int(self._config._get("reporting", "equity_curve_days") or 30)
        )

        # Phase progress (use latest equity from summaries or 0)
        current_equity: float = 0.0
        if equity_curve:
            current_equity = equity_curve[-1].get("account_equity_gbp", 0.0)

        phase: int = self._config.get_phase().value
        phase_progress: dict[str, Any] = self._performance.get_phase_progress(
            current_equity, phase
        )

        # Rolling metrics
        daily_returns: list[float] = self._performance.get_daily_returns(60)
        sharpe_ratio: float = self._performance.calculate_sharpe_ratio(daily_returns)

        # Open positions
        open_positions: list[dict[str, Any]] = self._get_open_positions()

        context: dict[str, Any] = {
            "date": report_date,
            "phase": phase,
            "phase_name": _phase_name(phase),
            "metrics": metrics,
            "ticker_stats": ticker_stats,
            "equity_curve": equity_curve,
            "phase_progress": phase_progress,
            "sharpe_ratio": round(sharpe_ratio, 2),
            "open_positions": open_positions,
            "account_equity_gbp": current_equity,
            "generated_at": datetime.now(TZ_EASTERN).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

        try:
            template = self._env.get_template("daily_report.html")
        except TemplateNotFound:
            logger.error("daily_report.html template not found in %s", self._templates_path)
            raise

        html: str = template.render(**context)
        filename: str = f"daily_{report_date}.html"
        return self._write_report(filename, html)

    # ------------------------------------------------------------------
    # Weekly report
    # ------------------------------------------------------------------

    def generate_weekly_report(self, week_ending: str) -> str:
        """Generate a weekly HTML report for the week ending on *week_ending*.

        Parameters
        ----------
        week_ending:
            ``YYYY-MM-DD`` of the Friday (or last trading day) of the week.

        Returns the absolute file path.
        """
        end_date: date = date.fromisoformat(week_ending)
        start_date: date = end_date - timedelta(days=4)  # Monday of the same week

        period_metrics: dict[str, Any] = self._performance.calculate_period_metrics(
            start_date.isoformat(), end_date.isoformat()
        )

        # Per-ticker stats for the week
        all_time_ticker_stats: list[dict[str, Any]] = self._performance.get_per_ticker_stats()

        # Equity curve
        equity_curve: list[dict[str, Any]] = self._performance.get_equity_curve(30)

        # Calculate ISO week number
        iso_year: int
        iso_week: int
        iso_year, iso_week, _ = end_date.isocalendar()

        phase: int = self._config.get_phase().value

        context: dict[str, Any] = {
            "week_ending": week_ending,
            "week_start": start_date.isoformat(),
            "iso_year": iso_year,
            "iso_week": iso_week,
            "phase": phase,
            "phase_name": _phase_name(phase),
            "period_metrics": period_metrics,
            "ticker_stats": all_time_ticker_stats,
            "equity_curve": equity_curve,
            "generated_at": datetime.now(TZ_EASTERN).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

        try:
            template = self._env.get_template("weekly_report.html")
        except TemplateNotFound:
            logger.error("weekly_report.html template not found in %s", self._templates_path)
            raise

        html: str = template.render(**context)
        filename: str = f"weekly_{iso_year}-W{iso_week:02d}.html"
        return self._write_report(filename, html)

    # ------------------------------------------------------------------
    # Monthly report
    # ------------------------------------------------------------------

    def generate_monthly_report(self, year: int, month: int) -> str:
        """Generate a monthly HTML report.

        Returns the absolute file path.
        """
        start_date: date = date(year, month, 1)
        # Last day of month
        if month == 12:
            end_date: date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)

        period_metrics: dict[str, Any] = self._performance.calculate_period_metrics(
            start_date.isoformat(), end_date.isoformat()
        )

        equity_curve: list[dict[str, Any]] = self._performance.get_equity_curve(60)

        daily_returns: list[float] = self._performance.get_daily_returns(60)
        sharpe_ratio: float = self._performance.calculate_sharpe_ratio(daily_returns)

        phase: int = self._config.get_phase().value
        current_equity: float = 0.0
        if equity_curve:
            current_equity = equity_curve[-1].get("account_equity_gbp", 0.0)

        phase_progress: dict[str, Any] = self._performance.get_phase_progress(
            current_equity, phase
        )

        # Compound growth target: 0.3-0.5% per day
        trading_days: int = period_metrics.get("trading_days", 0)
        target_low: float = current_equity * 0.003 * trading_days if current_equity > 0 else 0.0
        target_high: float = current_equity * 0.005 * trading_days if current_equity > 0 else 0.0

        context: dict[str, Any] = {
            "year": year,
            "month": month,
            "month_name": start_date.strftime("%B"),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "phase": phase,
            "phase_name": _phase_name(phase),
            "period_metrics": period_metrics,
            "equity_curve": equity_curve,
            "sharpe_ratio": round(sharpe_ratio, 2),
            "phase_progress": phase_progress,
            "target_low_gbp": round(target_low, 2),
            "target_high_gbp": round(target_high, 2),
            "account_equity_gbp": current_equity,
            "generated_at": datetime.now(TZ_EASTERN).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

        # Monthly report reuses weekly template if no dedicated template exists
        template_name: str = "monthly_report.html"
        try:
            template = self._env.get_template(template_name)
        except TemplateNotFound:
            logger.warning(
                "monthly_report.html not found, falling back to weekly_report.html"
            )
            template = self._env.get_template("weekly_report.html")

        html: str = template.render(**context)
        filename: str = f"monthly_{year}-{month:02d}.html"
        return self._write_report(filename, html)

    # ------------------------------------------------------------------
    # Phase 0 report
    # ------------------------------------------------------------------

    def generate_phase0_report(
        self,
        report_date: str,
        assessments: list[dict[str, Any]],
        portfolio: list[dict[str, Any]],
        account_equity_gbp: float,
        dry_run: bool = True,
        log_highlights: list[dict[str, str]] | None = None,
    ) -> str:
        """Generate a Phase 0 portfolio cleanup report.

        Parameters
        ----------
        report_date:
            ``YYYY-MM-DD`` date string.
        assessments:
            List of assessment dicts (from DB or PositionAssessment objects).
        portfolio:
            List of IB portfolio position dicts.
        account_equity_gbp:
            Current account equity in GBP.
        dry_run:
            Whether this was a dry-run execution.
        log_highlights:
            Optional list of ``{"level": "error"|"warning"|"info", "text": "..."}``
            entries to show in the report.

        Returns the absolute file path of the generated report.
        """
        import json

        # Parse scores_breakdown from JSON string if needed
        for a in assessments:
            if isinstance(a.get("scores_breakdown"), str):
                try:
                    a["scores_breakdown"] = json.loads(a["scores_breakdown"])
                except (json.JSONDecodeError, TypeError):
                    a["scores_breakdown"] = {}

        hold_count: int = sum(1 for a in assessments if a.get("classification") == "HOLD")
        sell_count: int = sum(1 for a in assessments if a.get("classification") == "SELL")
        urgent_count: int = sum(1 for a in assessments if a.get("classification") == "URGENT_SELL")
        scores: list[int] = [a.get("score", 0) for a in assessments]
        avg_score: float = sum(scores) / len(scores) if scores else 0.0
        total_unrealised: float = sum(p.get("unrealized_pnl", 0.0) for p in portfolio)

        context: dict[str, Any] = {
            "date": report_date,
            "dry_run": dry_run,
            "account_equity_gbp": account_equity_gbp,
            "portfolio": portfolio,
            "assessments": assessments,
            "total_unrealised_pnl": total_unrealised,
            "hold_count": hold_count,
            "sell_count": sell_count,
            "urgent_count": urgent_count,
            "avg_score": avg_score,
            "log_highlights": log_highlights or [],
            "generated_at": datetime.now(TZ_EASTERN).strftime("%Y-%m-%d %H:%M:%S ET"),
        }

        try:
            template = self._env.get_template("phase0_report.html")
        except TemplateNotFound:
            logger.error("phase0_report.html template not found in %s", self._templates_path)
            raise

        html: str = template.render(**context)
        filename: str = f"phase0_{report_date}.html"
        return self._write_report(filename, html)

    # ------------------------------------------------------------------
    # Data helpers (read from DB for context enrichment)
    # ------------------------------------------------------------------

    def _get_open_positions(self) -> list[dict[str, Any]]:
        """Fetch open positions from the database."""
        import sqlite3

        conn: sqlite3.Connection = sqlite3.connect(self._config.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM positions "
                "WHERE status NOT IN ('CLOSED', 'ENTRY_FAILED') "
                "ORDER BY entry_time"
            ).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            logger.warning("Could not query positions table")
            return []
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

def _fmt_pnl(value: float | None) -> str:
    """Format a P&L value with sign and colour hint."""
    if value is None:
        return "N/A"
    sign: str = "+" if value >= 0 else ""
    return f"{sign}{value:,.2f}"


def _fmt_pct(value: float | None, decimals: int = 1) -> str:
    """Format a decimal fraction as a percentage string."""
    if value is None:
        return "N/A"
    return f"{value * 100:.{decimals}f}%"


def _fmt_money(value: float | None) -> str:
    """Format a monetary value in GBP."""
    if value is None:
        return "N/A"
    return f"\u00a3{value:,.2f}"


def _fmt_time(value: str | None) -> str:
    """Format an ISO datetime string to a short display form."""
    if not value:
        return "N/A"
    try:
        dt: datetime = datetime.fromisoformat(value)
        return dt.strftime("%H:%M:%S")
    except (ValueError, TypeError):
        return str(value)


def _phase_name(phase: int) -> str:
    """Human-readable phase name."""
    names: dict[int, str] = {
        0: "Phase 0 - Portfolio Cleanup",
        1: "Phase 1 - Micro-Account Swing",
        2: "Phase 2 - Small Account Active",
        3: "Phase 3 - Full Day Trading",
    }
    return names.get(phase, f"Phase {phase}")

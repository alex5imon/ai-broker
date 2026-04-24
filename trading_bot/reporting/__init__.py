"""Reporting subsystem — performance metrics and HTML report generation."""

from .performance import PerformanceCalculator
from .daily_report import ReportGenerator

__all__: list[str] = ["PerformanceCalculator", "ReportGenerator"]

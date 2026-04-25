"""Shared logging configuration for all CLI entry points.

Sets up both console and file handlers so every command produces a clear log
file under ``trading_bot/logs/``.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_DIR: Path = Path(__file__).resolve().parent / "logs"
_LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(
    name: str,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> Path:
    """Configure root logger with console + rotating file handlers.

    Parameters
    ----------
    name:
        Basename for the log file (e.g. ``"multi_strategy_backtest"``).
        The file is written to ``trading_bot/logs/{name}.log``.
    level:
        Logging level for both handlers.
    max_bytes:
        Max size per log file before rotation (default 10 MB).
    backup_count:
        Number of rotated files to keep.

    Returns
    -------
    Path to the log file.
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path: Path = _LOG_DIR / f"{name}.log"

    root: logging.Logger = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers on repeated calls
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(file_handler)

    root.info("=" * 70)
    root.info("Log started: %s", name)
    root.info("Log file: %s", log_path)
    root.info("=" * 70)

    return log_path

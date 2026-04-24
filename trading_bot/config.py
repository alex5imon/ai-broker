"""Configuration loader with phase-aware access to all tunable parameters."""

from __future__ import annotations

import logging
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import yaml

from trading_bot.constants import HoldType, Market, Phase

logger: logging.Logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when the config file is invalid or missing required keys."""


class Config:
    """Loads config.yaml and exposes phase-aware, typed accessors.

    Usage::

        cfg = Config.load("config.yaml")
        phase = cfg.get_phase()
        max_pos = cfg.get_max_positions()
    """

    # Keys that MUST exist at the top level of config.yaml
    _REQUIRED_SECTIONS: list[str] = [
        "account",
        "alpaca",
        "market_data",
        "schedule",
        "holidays",
        "watchlist",
        "risk",
        "strategy",
        "entry",
        "sentiment",
        "exit_intraday",
        "exit_swing",
        "notifications",
        "settlement",
        "fx",
        "phase0",
        "phases",
        "reporting",
        "database",
        "logging",
    ]

    # -----------------------------------------------------------------------
    # Construction
    # -----------------------------------------------------------------------

    def __init__(self, raw: dict[str, Any], path: str | None = None) -> None:
        self._raw: dict[str, Any] = raw
        self._path: str | None = path
        self._phase: Phase | None = None  # cached after first resolution

    @classmethod
    def load(cls, path: str = "config.yaml") -> Config:
        """Load and validate *path*, returning a ready-to-use ``Config``."""
        resolved: Path = Path(path).expanduser().resolve()
        if not resolved.is_file():
            raise ConfigError(f"Config file not found: {resolved}")

        with open(resolved, "r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)

        if not isinstance(raw, dict):
            raise ConfigError(f"Config root must be a mapping, got {type(raw).__name__}")

        cfg = cls(raw, str(resolved))
        errors: list[str] = cfg.validate()
        if errors:
            for err in errors:
                logger.error("Config validation error: %s", err)
            raise ConfigError(
                f"Config has {len(errors)} validation error(s); first: {errors[0]}"
            )
        logger.info("Config loaded from %s (phase=%s)", resolved, cfg.get_phase().name)
        return cfg

    # -----------------------------------------------------------------------
    # Raw access helpers
    # -----------------------------------------------------------------------

    def _get(self, *keys: str, default: Any = None) -> Any:
        """Walk nested keys, returning *default* if any key is missing."""
        node: Any = self._raw
        for k in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(k, default)
            if node is default:
                return default
        return node

    def _require(self, *keys: str) -> Any:
        """Like ``_get`` but raises if the key path is missing."""
        val: Any = self._get(*keys)
        if val is None:
            raise ConfigError(f"Missing required config key: {'.'.join(keys)}")
        return val

    # -----------------------------------------------------------------------
    # Phase detection
    # -----------------------------------------------------------------------

    def get_phase(self, equity_gbp: float | None = None) -> Phase:
        """Return the current operating phase.

        Resolution order:
        1. ``account.phase_override`` (if set to an integer 0-3)
        2. Auto-detect from *equity_gbp* against phase thresholds
        3. If *equity_gbp* is ``None`` and no override, default to ``Phase.MICRO``
        """
        if self._phase is not None and equity_gbp is None:
            return self._phase

        override: int | None = self._get("account", "phase_override")
        if override is not None:
            phase = Phase(int(override))
            self._phase = phase
            return phase

        if equity_gbp is None:
            # No equity supplied and no override -> conservative default
            self._phase = Phase.MICRO
            return Phase.MICRO

        p2_threshold: float = float(self._require("phases", "phase1_to_phase2", "equity_gbp"))
        p3_threshold: float = float(self._require("phases", "phase2_to_phase3", "equity_gbp"))

        if equity_gbp >= p3_threshold:
            phase = Phase.FULL
        elif equity_gbp >= p2_threshold:
            phase = Phase.SMALL
        else:
            phase = Phase.MICRO

        self._phase = phase
        return phase

    def _phase_key(self) -> str:
        """Return the config sub-key for the current phase (e.g. ``'phase1'``)."""
        phase: Phase = self.get_phase()
        return f"phase{phase.value}"

    # -----------------------------------------------------------------------
    # Phase-aware accessors
    # -----------------------------------------------------------------------

    def get_risk_per_trade(self) -> float:
        """Return risk-per-trade percentage for the current phase (e.g. 0.02)."""
        return float(self._require("risk", "risk_per_trade_pct", self._phase_key()))

    def get_max_positions(self) -> int:
        """Maximum concurrent open positions for the current phase."""
        return int(self._require("risk", "max_positions", self._phase_key()))

    def get_max_daily_trades(self) -> int:
        """Maximum trades per day for the current phase."""
        return int(self._require("risk", "max_daily_trades", self._phase_key()))

    def get_max_sector_exposure(self) -> int:
        """Max positions in the same GICS sector for the current phase."""
        return int(self._require("risk", "max_sector_exposure", self._phase_key()))

    def get_max_position_pct(self) -> float:
        """Max position as a fraction of equity for the current phase."""
        return float(self._require("risk", "max_position_pct", self._phase_key()))

    def get_min_position_value(self, market: Market) -> float:
        """Minimum position value in USD."""
        pk: str = self._phase_key()
        return float(self._require("risk", "min_position_value", pk, "us_usd"))

    def get_scan_interval(self) -> int:
        """Strategy scan interval in seconds for the current phase."""
        return int(self._require("strategy", "scan_interval_seconds", self._phase_key()))

    def get_exit_params(self, hold_type: HoldType) -> dict[str, Any]:
        """Return exit parameters dict for the given *hold_type*, phase-aware.

        For intraday trades, phase 2/3 overrides are applied on top of base
        parameters.  For swing trades, the same parameters are used in all
        phases.
        """
        if hold_type == HoldType.SWING:
            base: dict[str, Any] = dict(self._require("exit_swing"))
            return base

        # Intraday: start with base, then overlay phase override if present
        base = dict(self._require("exit_intraday"))
        phase: Phase = self.get_phase()
        override_section: str | None = None
        if phase == Phase.SMALL:
            override_section = "exit_intraday_phase2"
        elif phase == Phase.FULL:
            override_section = "exit_intraday_phase3"

        if override_section is not None:
            overrides: dict[str, Any] | None = self._get(override_section)
            if overrides and isinstance(overrides, dict):
                base.update(overrides)
        return base

    # -----------------------------------------------------------------------
    # Watchlist
    # -----------------------------------------------------------------------

    def get_watchlist(self, market: Market) -> list[str]:
        """Return the combined US watchlist based on the current phase."""
        phase: Phase = self.get_phase()
        tickers: list[str] = list(self._get("watchlist", "us") or [])
        if phase >= Phase.SMALL:
            tickers.extend(self._get("watchlist", "us_phase2") or [])
        if phase >= Phase.FULL:
            tickers.extend(self._get("watchlist", "us_phase3") or [])
        return tickers

    # -----------------------------------------------------------------------
    # Schedule helpers
    # -----------------------------------------------------------------------

    def _schedule_section(self, market: Market) -> dict[str, Any]:
        section: Any = self._get("schedule", "us")
        if not isinstance(section, dict):
            raise ConfigError("Missing schedule section for 'us'")
        return section

    def get_market_open(self, market: Market) -> time:
        """Return market open as a ``time`` object."""
        return _parse_time(self._schedule_section(market)["market_open"])

    def get_execution_start(self, market: Market) -> time:
        return _parse_time(self._schedule_section(market)["execution_start"])

    def get_execution_end(self, market: Market) -> time:
        return _parse_time(self._schedule_section(market)["execution_end"])

    def get_wind_down_start(self, market: Market) -> time:
        return _parse_time(self._schedule_section(market)["wind_down_start"])

    def get_wind_down_end(self, market: Market) -> time:
        return _parse_time(self._schedule_section(market)["wind_down_end"])

    def get_market_close(self, market: Market) -> time:
        return _parse_time(self._schedule_section(market)["market_close"])

    def is_trading_day(self, d: date, market: Market) -> bool:
        """Return ``True`` if *d* is a normal trading day for *market*.

        Checks weekends and the holiday calendar from config.
        """
        # Weekends are never trading days
        if d.weekday() >= 5:
            return False

        year_suffix: str = str(d.year)
        holidays_key: str = f"us_{year_suffix}"

        holiday_dates: list[str] | None = self._get("holidays", holidays_key)
        if holiday_dates:
            date_str: str = d.isoformat()
            if date_str in holiday_dates:
                return False

        return True

    def is_early_close(self, d: date, market: Market) -> bool:
        """Return ``True`` if *d* is an early-close day."""
        year_suffix: str = str(d.year)
        early_dates: list[str] | None = self._get("holidays", f"us_early_close_{year_suffix}")
        if early_dates:
            return d.isoformat() in early_dates
        return False

    # -----------------------------------------------------------------------
    # Convenience accessors (non-phase-aware)
    # -----------------------------------------------------------------------

    @property
    def account_id(self) -> str:
        return str(self._get("account", "id", default=""))

    @property
    def base_currency(self) -> str:
        return str(self._require("account", "base_currency"))

    @property
    def trading_mode(self) -> str:
        return str(self._require("account", "trading_mode"))

    @property
    def alpaca_paper(self) -> bool:
        return bool(self._get("alpaca", "paper", default=False))

    @property
    def db_path(self) -> str:
        return str(self._get("database", "path", default="trading_bot/data/trading_bot.db"))

    @property
    def daily_loss_limit_pct(self) -> float:
        return float(self._require("risk", "daily_loss_limit_pct"))

    @property
    def drawdown_breaker_threshold(self) -> float:
        return float(self._require("risk", "drawdown_breaker", "threshold_pct"))

    @property
    def drawdown_breaker_rolling_days(self) -> int:
        return int(self._require("risk", "drawdown_breaker", "rolling_days"))

    @property
    def drawdown_breaker_pause_days(self) -> int:
        return int(self._require("risk", "drawdown_breaker", "pause_days"))

    @property
    def correlation_threshold(self) -> float:
        return float(self._require("risk", "correlation_threshold"))

    @property
    def entry_cooldown_minutes(self) -> int:
        return int(self._require("entry", "cooldown_minutes"))

    @property
    def earnings_blackout_hours(self) -> int:
        return int(self._require("entry", "earnings_blackout_hours"))

    @property
    def sentiment_cache_ttl(self) -> int:
        return int(self._require("sentiment", "cache_ttl_minutes"))

    @property
    def settlement_t_plus_days(self) -> int:
        return int(self._get("settlement", "t_plus_days", default=1))

    @property
    def fx_fallback_gbp_usd(self) -> float:
        return float(self._get("fx", "fallback_gbp_usd", default=1.27))

    @property
    def log_level(self) -> str:
        return str(self._get("logging", "level", default="INFO"))

    @property
    def log_file(self) -> str:
        return str(self._get("logging", "file", default="trading_bot/logs/bot.log"))

    @property
    def log_max_bytes(self) -> int:
        return int(self._get("logging", "max_bytes", default=10_485_760))

    @property
    def log_backup_count(self) -> int:
        return int(self._get("logging", "backup_count", default=5))

    @property
    def log_format(self) -> str:
        return str(
            self._get(
                "logging",
                "format",
                default="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        )

    @property
    def ntfy_server(self) -> str:
        return str(self._get("notifications", "ntfy_server", default="https://ntfy.sh"))

    @property
    def ntfy_topic(self) -> str:
        return str(self._require("notifications", "ntfy_topic"))

    @property
    def ntfy_kill_topic(self) -> str:
        return str(self._require("notifications", "ntfy_kill_topic"))

    @property
    def health_enabled(self) -> bool:
        return bool(self._get("health", "enabled", default=True))

    @property
    def health_host(self) -> str:
        return str(self._get("health", "host", default="0.0.0.0"))

    @property
    def health_port(self) -> int:
        return int(self._get("health", "port", default=8080))

    @property
    def report_output_dir(self) -> str:
        return str(self._get("reporting", "output_dir", default="~/trading_bot_reports"))

    # -----------------------------------------------------------------------
    # Validation
    # -----------------------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty == valid)."""
        errors: list[str] = []

        # Required top-level sections
        for section in self._REQUIRED_SECTIONS:
            if section not in self._raw:
                errors.append(f"Missing required section: '{section}'")

        if errors:
            # Can't validate deeper if top-level sections are missing
            return errors

        # Numeric range checks
        _check_positive(errors, self._get("risk", "daily_loss_limit_pct"), "risk.daily_loss_limit_pct")

        for pk in ("phase1", "phase2", "phase3"):
            _check_positive(errors, self._get("risk", "risk_per_trade_pct", pk), f"risk.risk_per_trade_pct.{pk}")
            _check_positive(errors, self._get("risk", "max_positions", pk), f"risk.max_positions.{pk}")
            _check_positive(errors, self._get("risk", "max_daily_trades", pk), f"risk.max_daily_trades.{pk}")
            _check_positive(errors, self._get("risk", "max_sector_exposure", pk), f"risk.max_sector_exposure.{pk}")

            pct = self._get("risk", "max_position_pct", pk)
            if pct is not None and (float(pct) <= 0 or float(pct) > 1):
                errors.append(f"risk.max_position_pct.{pk} must be in (0, 1], got {pct}")

        # Stop-loss / take-profit must be positive
        for section_name in ("exit_intraday", "exit_swing"):
            section = self._get(section_name)
            if isinstance(section, dict):
                _check_positive(errors, section.get("stop_loss_pct"), f"{section_name}.stop_loss_pct")
                _check_positive(errors, section.get("take_profit_pct"), f"{section_name}.take_profit_pct")

        # Phase transition thresholds must be positive and ordered
        p2_eq = self._get("phases", "phase1_to_phase2", "equity_gbp")
        p3_eq = self._get("phases", "phase2_to_phase3", "equity_gbp")
        if p2_eq is not None and p3_eq is not None:
            if float(p3_eq) <= float(p2_eq):
                errors.append(
                    f"Phase 3 equity threshold ({p3_eq}) must exceed Phase 2 ({p2_eq})"
                )

        # Watchlist must not be empty
        if not self._get("watchlist", "us"):
            errors.append("Watchlist is empty — at least one US ticker required")

        return errors

    # -----------------------------------------------------------------------
    # -----------------------------------------------------------------------
    # Multi-strategy accessors
    # -----------------------------------------------------------------------

    @property
    def multi_strategy_enabled(self) -> bool:
        return bool(self._get("multi_strategy", "enabled", default=False))

    @property
    def multi_strategy_total_allocation(self) -> float:
        return float(self._get("multi_strategy", "total_allocation_usd", default=1000.0))

    @property
    def multi_strategy_comparison_days(self) -> int:
        return int(self._get("multi_strategy", "comparison_period_days", default=30))

    def get_strategy_configs(self) -> dict[str, dict[str, Any]]:
        """Return per-strategy configs from multi_strategy.strategies."""
        return dict(self._get("multi_strategy", "strategies", default={}) or {})

    def get_strategy_config(self, strategy_id: str) -> dict[str, Any]:
        """Return config for a single strategy."""
        strategies: dict[str, Any] = self.get_strategy_configs()
        return dict(strategies.get(strategy_id, {}))

    # Repr
    # -----------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"Config(path={self._path!r}, phase={self.get_phase().name})"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse_time(value: str) -> time:
    """Parse ``'HH:MM'`` string into a ``time`` object."""
    parts = value.strip().split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


def _check_positive(errors: list[str], value: Any, label: str) -> None:
    """Append an error if *value* is not a positive number."""
    if value is None:
        errors.append(f"{label} is missing")
        return
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"{label} must be a number, got {value!r}")
        return
    if v <= 0:
        errors.append(f"{label} must be > 0, got {v}")

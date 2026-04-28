"""Walkforward harness + bootstrap confidence intervals.

Borrowed in spirit from edtechre/pybroker's walkforward analysis. The aim is
to convert a single-window backtest result (e.g. SPY mean reversion 2008-2021,
PF 1.54) into an honest out-of-sample distribution: split the date range into
N rolling test windows, run the backtester on each, and aggregate per-strategy
metrics with a non-parametric confidence interval over trade returns.

Two layers:

1. ``run_walkforward`` — slices a [from, to] range into ``window_days`` test
   windows stepped by ``step_days``, runs a caller-supplied
   ``run_window(d_from, d_to)`` coroutine on each, and aggregates the
   per-window ``MultiStrategyResult`` outputs.

2. ``bootstrap_metric_ci`` — non-parametric percentile bootstrap on a list of
   trade returns, producing a CI for an arbitrary metric callable.

This module deliberately avoids parameter retraining. The strategies in
config.yaml are evaluated as-is on each test window; the OOS aggregate then
shows whether the headline single-window metric holds across the full range
or hinges on one favourable regime.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Awaitable, Callable, Sequence

from trading_bot.multi_strategy_backtest import (
    MultiStrategyResult,
    StrategyResult,
    StrategyTrade,
)

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WalkforwardConfig:
    """Configuration for a walkforward run.

    Attributes:
        window_days: Length of each rolling test window in calendar days.
        step_days: Days to step the window forward between iterations. If
            equal to ``window_days``, windows are non-overlapping. If less,
            windows overlap (more samples but correlated).
        min_trades_per_window: Skip per-window stats if a strategy didn't
            generate at least this many trades. Aggregate stats still include
            the trades.
        bootstrap_samples: Number of bootstrap resamples for CI. 1000 gives
            stable percentiles without dominating runtime.
        bootstrap_ci: Two-sided coverage. 0.95 → reports 2.5/97.5 percentiles.
        random_seed: Seed for the bootstrap RNG so results are reproducible.
    """

    window_days: int = 90
    step_days: int = 90
    min_trades_per_window: int = 5
    bootstrap_samples: int = 1000
    bootstrap_ci: float = 0.95
    random_seed: int = 0


@dataclass(frozen=True)
class BootstrapCI:
    """Percentile-bootstrap confidence interval for a scalar metric."""

    metric: str
    point_estimate: float
    lower: float
    upper: float
    samples: int

    def as_dict(self) -> dict[str, float | int | str]:
        return {
            "metric": self.metric,
            "point_estimate": round(self.point_estimate, 4),
            "lower": round(self.lower, 4),
            "upper": round(self.upper, 4),
            "samples": self.samples,
        }


@dataclass(frozen=True)
class StrategyWindowStats:
    """Per-window summary for a single strategy."""

    window_idx: int
    from_date: str
    to_date: str
    trades: int
    return_pct: float
    win_rate: float
    profit_factor: float | None
    max_drawdown_pct: float


@dataclass
class WindowResult:
    """Wraps the raw MultiStrategyResult plus its window index."""

    window_idx: int
    from_date: date
    to_date: date
    result: MultiStrategyResult


@dataclass
class WalkforwardResult:
    """Aggregate output of a walkforward run."""

    config: WalkforwardConfig
    from_date: str
    to_date: str
    windows: list[WindowResult] = field(default_factory=list)
    # strategy_id -> per-window stats (one row per window where ran)
    per_window: dict[str, list[StrategyWindowStats]] = field(default_factory=dict)
    # strategy_id -> aggregated OOS stats across all windows
    aggregate: dict[str, dict[str, float | int | None]] = field(default_factory=dict)
    # strategy_id -> {metric_name: BootstrapCI}
    bootstrap: dict[str, dict[str, BootstrapCI]] = field(default_factory=dict)

    def summary(self) -> str:
        lines: list[str] = [
            "Walkforward summary",
            f"  Range: {self.from_date} to {self.to_date}",
            f"  Windows: {len(self.windows)} "
            f"(window={self.config.window_days}d, step={self.config.step_days}d)",
            "",
        ]
        for sid, agg in self.aggregate.items():
            lines.append(f"  Strategy: {sid}")
            lines.append(
                f"    Trades: {agg['trades']}  "
                f"WinRate: {_fmt_pct(agg.get('win_rate'))}  "
                f"PF: {_fmt_num(agg.get('profit_factor'))}  "
                f"OOS Return: {_fmt_pct(agg.get('return_pct'))}  "
                f"Sharpe: {_fmt_num(agg.get('sharpe_approx'))}"
            )
            cis = self.bootstrap.get(sid, {})
            for metric_name, ci in cis.items():
                lines.append(
                    f"    {metric_name} CI {int(self.config.bootstrap_ci * 100)}%: "
                    f"[{ci.lower:.3f}, {ci.upper:.3f}] "
                    f"(point={ci.point_estimate:.3f})"
                )
            lines.append("")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


WindowRunner = Callable[[date, date], Awaitable[MultiStrategyResult]]


async def run_walkforward(
    from_date: date,
    to_date: date,
    run_window: WindowRunner,
    config: WalkforwardConfig | None = None,
) -> WalkforwardResult:
    """Run rolling-window OOS evaluation.

    Args:
        from_date: Inclusive start.
        to_date: Inclusive end.
        run_window: Async callable that runs the underlying backtest for a
            single window and returns its ``MultiStrategyResult``.
        config: Walkforward parameters; defaults if not supplied.
    """
    cfg: WalkforwardConfig = config or WalkforwardConfig()
    if cfg.window_days <= 0 or cfg.step_days <= 0:
        raise ValueError("window_days and step_days must be positive")

    windows: list[WindowResult] = []
    cursor: date = from_date
    idx: int = 0
    while cursor <= to_date:
        window_end: date = min(cursor + timedelta(days=cfg.window_days - 1), to_date)
        if (window_end - cursor).days < max(cfg.window_days // 4, 1):
            # Skip a tiny trailing window; aggregate would be too noisy.
            break

        logger.info(
            "Walkforward window %d: %s -> %s",
            idx + 1, cursor.isoformat(), window_end.isoformat(),
        )
        try:
            window_result: MultiStrategyResult = await run_window(cursor, window_end)
        except Exception:
            logger.exception(
                "Window %d failed (%s -> %s) — skipping",
                idx + 1, cursor.isoformat(), window_end.isoformat(),
            )
            cursor = cursor + timedelta(days=cfg.step_days)
            idx += 1
            continue

        windows.append(
            WindowResult(
                window_idx=idx,
                from_date=cursor,
                to_date=window_end,
                result=window_result,
            )
        )
        cursor = cursor + timedelta(days=cfg.step_days)
        idx += 1

    out: WalkforwardResult = WalkforwardResult(
        config=cfg,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        windows=windows,
    )
    if not windows:
        logger.warning("Walkforward produced no windows")
        return out

    _aggregate(out, cfg)
    return out


def bootstrap_metric_ci(
    trade_returns: Sequence[float],
    metric: Callable[[Sequence[float]], float],
    *,
    metric_name: str,
    samples: int = 1000,
    coverage: float = 0.95,
    seed: int = 0,
) -> BootstrapCI | None:
    """Percentile-bootstrap CI for a metric over trade-return resamples.

    Args:
        trade_returns: Per-trade fractional returns (e.g. PnL / cost basis).
        metric: Callable that maps a resampled list to a scalar (e.g. PF,
            mean, Sharpe-approx).
        metric_name: Label for the result.
        samples: Number of bootstrap resamples (with replacement).
        coverage: Two-sided CI coverage (e.g. 0.95 → 2.5/97.5 percentiles).
        seed: RNG seed for reproducibility.

    Returns:
        ``BootstrapCI`` or ``None`` if there aren't enough trades to be useful.
    """
    n: int = len(trade_returns)
    if n < 5:
        return None
    if not 0.0 < coverage < 1.0:
        raise ValueError(f"coverage must be in (0, 1), got {coverage}")

    rng: random.Random = random.Random(seed)
    point: float = metric(list(trade_returns))

    estimates: list[float] = []
    for _ in range(samples):
        resample: list[float] = [rng.choice(trade_returns) for _ in range(n)]
        try:
            estimates.append(metric(resample))
        except (ZeroDivisionError, ValueError):
            continue

    if not estimates:
        return None

    estimates.sort()
    alpha: float = (1.0 - coverage) / 2.0
    lower: float = _percentile(estimates, alpha)
    upper: float = _percentile(estimates, 1.0 - alpha)
    return BootstrapCI(
        metric=metric_name,
        point_estimate=point,
        lower=lower,
        upper=upper,
        samples=len(estimates),
    )


# ---------------------------------------------------------------------------
# Built-in metrics for trade-return bootstrapping
# ---------------------------------------------------------------------------


def metric_profit_factor(returns: Sequence[float]) -> float:
    """Sum(positive) / |Sum(negative)|. Returns +inf if no losses, 0 if all zero."""
    gains: float = sum(r for r in returns if r > 0)
    losses: float = -sum(r for r in returns if r < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def metric_win_rate(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    return sum(1 for r in returns if r > 0) / len(returns)


def metric_mean_return(returns: Sequence[float]) -> float:
    return statistics.fmean(returns) if returns else 0.0


def metric_sharpe_approx(returns: Sequence[float]) -> float:
    """Simple Sharpe approximation: mean / stdev. Per-trade, no annualisation."""
    if len(returns) < 2:
        return 0.0
    mean: float = statistics.fmean(returns)
    sd: float = statistics.pstdev(returns)
    if sd == 0:
        return 0.0
    return mean / sd


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _aggregate(out: WalkforwardResult, cfg: WalkforwardConfig) -> None:
    # Index per-strategy trades and per-window stats.
    by_strategy: dict[str, list[StrategyTrade]] = {}
    by_strategy_returns: dict[str, list[float]] = {}
    display_name: dict[str, str] = {}

    for window in out.windows:
        for sr in window.result.strategies:
            sid: str = sr.strategy_id
            display_name.setdefault(sid, sr.display_name)

            by_strategy.setdefault(sid, []).extend(sr.trades)
            by_strategy_returns.setdefault(sid, []).extend(
                _trade_returns(sr)
            )

            stats: StrategyWindowStats = StrategyWindowStats(
                window_idx=window.window_idx,
                from_date=window.from_date.isoformat(),
                to_date=window.to_date.isoformat(),
                trades=sr.total_trades,
                return_pct=sr.return_pct,
                win_rate=sr.win_rate,
                profit_factor=sr.profit_factor,
                max_drawdown_pct=sr.max_drawdown_pct,
            )
            out.per_window.setdefault(sid, []).append(stats)

    # Aggregate metrics across all OOS trades.
    for sid, trades in by_strategy.items():
        returns: list[float] = by_strategy_returns[sid]
        wins: int = sum(1 for r in returns if r > 0)
        losses: int = sum(1 for r in returns if r < 0)
        total: int = len(returns)
        gross_pnl: float = sum(t.net_pnl_usd for t in trades)
        # OOS return: compound the per-window returns (geometric, more
        # realistic than summing).
        compounded_return_pct: float = _compound_window_returns(out.per_window[sid])

        per_window_stats: list[StrategyWindowStats] = out.per_window.get(sid, [])

        out.aggregate[sid] = {
            "display_name": display_name[sid],
            "trades": total,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total * 100.0) if total else 0.0,
            "profit_factor": metric_profit_factor(returns) if total else None,
            "return_pct": compounded_return_pct,
            "total_pnl_usd": gross_pnl,
            "sharpe_approx": metric_sharpe_approx(returns),
            "windows_traded": sum(1 for w in per_window_stats if w.trades > 0),
        }

    # Bootstrap CIs.
    for sid, returns in by_strategy_returns.items():
        if len(returns) < 5:
            continue
        cis: dict[str, BootstrapCI] = {}
        for metric_name, fn in (
            ("profit_factor", metric_profit_factor),
            ("win_rate", metric_win_rate),
            ("mean_return", metric_mean_return),
            ("sharpe_approx", metric_sharpe_approx),
        ):
            ci = bootstrap_metric_ci(
                returns,
                metric=fn,
                metric_name=metric_name,
                samples=cfg.bootstrap_samples,
                coverage=cfg.bootstrap_ci,
                seed=cfg.random_seed,
            )
            if ci is not None:
                cis[metric_name] = ci
        out.bootstrap[sid] = cis


def _trade_returns(sr: StrategyResult) -> list[float]:
    """Per-trade fractional return (pnl / cost basis)."""
    out: list[float] = []
    for t in sr.trades:
        cost: float = t.entry_price * float(t.shares)
        if cost <= 0 or t.exit_price is None:
            continue
        out.append(t.net_pnl_usd / cost)
    return out


def _compound_window_returns(stats: list[StrategyWindowStats]) -> float:
    """Compound a list of per-window percent returns geometrically."""
    factor: float = 1.0
    for s in stats:
        factor *= 1.0 + (s.return_pct / 100.0)
    return (factor - 1.0) * 100.0


def _percentile(sorted_values: list[float], q: float) -> float:
    """Linear-interpolation percentile on a sorted list."""
    if not sorted_values:
        return float("nan")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos: float = q * (len(sorted_values) - 1)
    lo: int = int(math.floor(pos))
    hi: int = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac: float = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _fmt_pct(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):+.2f}%"


def _fmt_num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.3f}"

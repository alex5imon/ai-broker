"""A/B harness for the calendar-effect overlay (issue ai-broker#47).

Runs six backtests against the 13-ETF universe over 2020-07-27 to
2026-04-16 (the issue's specified window):

  1. baseline  — overlay disabled (--no-calendar-overlay)
  2. turn_of_month only
  3. fomc_drift only
  4. pre_long_weekend only
  5. opex only
  6. composite — all four overlays enabled

Per-overlay config is written to a temp YAML so the live config.yaml is
never mutated. Backtest stdout is captured to per-run log files; the
JSON result paths emitted by the backtester are gathered for reporting.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
REPORTS_DIR: Path = PROJECT_ROOT / "backtest_results" / "calendar_overlay_ab"

UNIVERSE: str = "SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC"
DATE_FROM: str = "2020-07-27"
DATE_TO: str = "2026-04-16"


@dataclass(frozen=True)
class RunSpec:
    name: str
    overrides: dict
    no_overlay_flag: bool = False


def _enable(only: str | None) -> dict:
    """Return calendar_overlay block with master on and ``only`` enabled."""
    base = {
        "calendar_overlay": {
            "enabled": True,
            "turn_of_month": {
                "enabled": only == "turn_of_month",
                "days_before_month_end": 4,
                "days_after_month_start": 3,
                "long_multiplier": 1.2,
                "short_multiplier": 0.8,
                "applies_to": ["mean_reversion", "overnight_drift"],
            },
            "fomc_drift": {
                "enabled": only == "fomc_drift",
                "hours_before_announcement": 24,
                "long_multiplier": 1.3,
                "applies_to": ["overnight_drift"],
            },
            "pre_long_weekend": {
                "enabled": only == "pre_long_weekend",
                "min_weekend_days": 4,  # see PR notes — 4 = true long weekend
                "block_strategies": ["overnight_drift"],
            },
            "opex": {
                "enabled": only == "opex",
                "weekday": 4,
                "week_of_month": 3,
                "multiplier": 0.7,
                "applies_to": ["mean_reversion", "overnight_drift"],
            },
        }
    }
    if only == "all":
        for k in ("turn_of_month", "fomc_drift", "pre_long_weekend", "opex"):
            base["calendar_overlay"][k]["enabled"] = True
    return base


RUNS: list[RunSpec] = [
    RunSpec("baseline", {}, no_overlay_flag=True),
    RunSpec("turn_of_month", _enable("turn_of_month")),
    RunSpec("fomc_drift", _enable("fomc_drift")),
    RunSpec("pre_long_weekend", _enable("pre_long_weekend")),
    RunSpec("opex", _enable("opex")),
    RunSpec("composite_all", _enable("all")),
]


def _write_temp_config(name: str, overrides: dict) -> Path:
    src: Path = PROJECT_ROOT / "config.yaml"
    with src.open() as f:
        cfg = yaml.safe_load(f)
    if overrides:
        merged = copy.deepcopy(cfg)
        for k, v in overrides.items():
            merged[k] = v
    else:
        merged = cfg
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out: Path = REPORTS_DIR / f"config_{name}.yaml"
    with out.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False)
    return out


def _run_one(spec: RunSpec) -> dict:
    cfg_path: Path = _write_temp_config(spec.name, spec.overrides)
    log_path: Path = REPORTS_DIR / f"{spec.name}.log"
    cmd: list[str] = [
        sys.executable, "-m", "trading_bot.multi_strategy_backtest",
        "--from", DATE_FROM, "--to", DATE_TO,
        "--multi-intraday",
        "--tickers", UNIVERSE,
        "--config", str(cfg_path),
        # Daily-bar cache only covers Dec 2025+; running the regime
        # filter against a 2020-2026 window would block every entry on
        # 2020-2025 for lack of SMA50 history. The A/B is on the
        # overlay's effect, not the regime filter, so disable it.
        "--no-regime-filter",
    ]
    if spec.no_overlay_flag:
        cmd.append("--no-calendar-overlay")

    print(f"\n=== {spec.name} ===\n  cmd: {' '.join(cmd)}", flush=True)
    with log_path.open("w") as logf:
        proc = subprocess.run(
            cmd, cwd=PROJECT_ROOT, stdout=logf, stderr=subprocess.STDOUT,
            text=True,
        )
    print(f"  exit={proc.returncode}  log={log_path}", flush=True)

    output: str = log_path.read_text()
    json_match = re.search(r"Results saved to:\s*(\S+\.json)", output)
    json_path: Path | None = Path(json_match.group(1)) if json_match else None

    summary: dict = {
        "name": spec.name,
        "exit_code": proc.returncode,
        "log": str(log_path),
        "json": str(json_path) if json_path else None,
    }
    if json_path and json_path.exists():
        try:
            with json_path.open() as f:
                data = json.load(f)
            summary["totals"] = _extract_totals(data)
        except Exception as e:
            summary["totals_error"] = str(e)
    return summary


def _extract_totals(data: dict) -> dict:
    """Pull headline metrics per strategy from the backtester JSON."""
    out: dict = {}
    for s in data.get("strategies", []) or []:
        sid = s.get("strategy_id")
        if not sid:
            continue
        out[sid] = {
            "trades": s.get("total_trades"),
            "wins": s.get("wins"),
            "return_pct": s.get("return_pct"),
            "sharpe": s.get("sharpe_approx"),
            "max_dd_pct": s.get("max_drawdown_pct"),
            "win_rate": s.get("win_rate"),
            "profit_factor": s.get("profit_factor"),
        }
    return out


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    summary_path: Path = REPORTS_DIR / "summary.json"

    summaries: list[dict] = []
    for spec in RUNS:
        summaries.append(_run_one(spec))
        with summary_path.open("w") as f:
            json.dump(summaries, f, indent=2, default=str)

    print(f"\n=== A/B summary written to {summary_path} ===")
    for s in summaries:
        print(f"\n[{s['name']}] exit={s['exit_code']}")
        for sid, m in (s.get("totals") or {}).items():
            print(f"  {sid}: {m}")


if __name__ == "__main__":
    main()

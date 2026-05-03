"""Walkforward A/B for pre_long_weekend overlay (issue ai-broker#47).

Per-window OOS check called for in the issue's acceptance criteria:

* OOS Return improvement positive across ≥ 4 of 6 yearly windows
* No single-window catastrophic loss (no window worse than baseline by > 200 bps)

Runs two walkforward backtests over the issue's window:

  baseline: --no-calendar-overlay
  prelw_on: calendar_overlay.enabled + pre_long_weekend.enabled,
            min_weekend_days=4 (true long weekends only)

Both runs use ``--no-regime-filter`` for the same reason as the
aggregate A/B — daily-bar cache is sparse pre-2026.
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
REPORTS_DIR: Path = PROJECT_ROOT / "backtest_results" / "calendar_overlay_walkforward"

UNIVERSE: str = "SPY,QQQ,XLF,XLK,XLE,XLV,XLI,XLY,XLP,XLU,XLB,XLRE,XLC"
DATE_FROM: str = "2020-07-27"
DATE_TO: str = "2026-04-16"

# 6 yearly OOS windows over the ~5.7 year span.
WINDOW_DAYS: int = 365
STEP_DAYS: int = 365


@dataclass(frozen=True)
class RunSpec:
    name: str
    overlay_enabled: bool
    no_overlay_flag: bool


def _build_config(name: str, overlay_enabled: bool) -> Path:
    src: Path = PROJECT_ROOT / "config.yaml"
    with src.open() as f:
        cfg = yaml.safe_load(f)
    cfg = copy.deepcopy(cfg)
    cfg["calendar_overlay"] = {
        "enabled": overlay_enabled,
        "turn_of_month": {
            "enabled": False,
            "days_before_month_end": 4,
            "days_after_month_start": 3,
            "long_multiplier": 1.2,
            "short_multiplier": 0.8,
            "applies_to": ["mean_reversion", "overnight_drift"],
        },
        "fomc_drift": {
            "enabled": False,
            "hours_before_announcement": 24,
            "long_multiplier": 1.3,
            "applies_to": ["overnight_drift"],
        },
        "pre_long_weekend": {
            "enabled": overlay_enabled,
            "min_weekend_days": 4,
            "block_strategies": ["overnight_drift"],
        },
        "opex": {
            "enabled": False,
            "weekday": 4,
            "week_of_month": 3,
            "multiplier": 0.7,
            "applies_to": ["mean_reversion", "overnight_drift"],
        },
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out: Path = REPORTS_DIR / f"config_{name}.yaml"
    with out.open("w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)
    return out


def _run_one(spec: RunSpec) -> dict:
    cfg_path: Path = _build_config(spec.name, spec.overlay_enabled)
    log_path: Path = REPORTS_DIR / f"{spec.name}.log"
    cmd: list[str] = [
        sys.executable, "-m", "trading_bot.multi_strategy_backtest",
        "--from", DATE_FROM, "--to", DATE_TO,
        "--multi-intraday",
        "--tickers", UNIVERSE,
        "--config", str(cfg_path),
        "--no-regime-filter",
        "--walkforward",
        "--wf-window", str(WINDOW_DAYS),
        "--wf-step", str(STEP_DAYS),
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
    json_match = re.search(r"Walkforward (?:JSON saved to|results saved to:)\s*(\S+\.json)", output)
    json_path: Path | None = Path(json_match.group(1)) if json_match else None

    return {
        "name": spec.name,
        "exit_code": proc.returncode,
        "log": str(log_path),
        "json": str(json_path) if json_path else None,
    }


def _load_per_window(json_path: str) -> dict[str, list[dict]]:
    with Path(json_path).open() as f:
        data = json.load(f)
    return data.get("per_window", {})


def _evaluate(baseline_json: str, prelw_json: str) -> dict:
    base_pw = _load_per_window(baseline_json)
    prelw_pw = _load_per_window(prelw_json)
    sleeve = "overnight_drift"  # the only sleeve pre_long_weekend touches

    base_windows = base_pw.get(sleeve, [])
    prelw_windows = prelw_pw.get(sleeve, [])
    pairs: list[dict] = []
    for b, p in zip(base_windows, prelw_windows):
        delta_ret = round(p["return_pct"] - b["return_pct"], 4)
        pairs.append({
            "window_idx": b["window_idx"],
            "from": b["from_date"],
            "to": b["to_date"],
            "baseline_return_pct": b["return_pct"],
            "prelw_return_pct": p["return_pct"],
            "delta_pp": delta_ret,
            "trades_baseline": b["trades"],
            "trades_prelw": p["trades"],
        })

    positive_windows = sum(1 for x in pairs if x["delta_pp"] > 0)
    catastrophic = [x for x in pairs if x["delta_pp"] < -2.0]
    return {
        "sleeve": sleeve,
        "total_windows": len(pairs),
        "positive_windows": positive_windows,
        "catastrophic_windows": catastrophic,
        "passes_4_of_6": positive_windows >= 4 and len(pairs) >= 4,
        "passes_no_catastrophic": not catastrophic,
        "windows": pairs,
    }


def main() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    runs = [
        RunSpec("baseline", overlay_enabled=False, no_overlay_flag=True),
        RunSpec("prelw_on", overlay_enabled=True, no_overlay_flag=False),
    ]
    summaries = [_run_one(s) for s in runs]

    summary_path = REPORTS_DIR / "summary.json"
    if all(s["json"] for s in summaries):
        analysis = _evaluate(summaries[0]["json"], summaries[1]["json"])
        out = {"runs": summaries, "analysis": analysis}
    else:
        out = {"runs": summaries, "analysis": "missing json — see logs"}
    with summary_path.open("w") as f:
        json.dump(out, f, indent=2)
    print(f"\n=== summary written to {summary_path} ===")
    if isinstance(out["analysis"], dict):
        a = out["analysis"]
        print(f"  total_windows={a['total_windows']}")
        print(f"  positive_windows={a['positive_windows']}")
        print(f"  passes_4_of_6={a['passes_4_of_6']}")
        print(f"  passes_no_catastrophic={a['passes_no_catastrophic']}")


if __name__ == "__main__":
    main()

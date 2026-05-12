"""Unit tests for scripts/walkforward_health_check.py.

The script is the gatekeeper between the weekly walkforward backtest
and the alert PR / artifact. A formatting bug here directly affects
operator-visible reports (and our willingness to act on alerts).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ isn't a package — load the module directly. Register in
# sys.modules BEFORE exec so the @dataclass decorator on StrategyHealth
# can resolve __module__ via sys.modules lookup (Python 3.13+ tightened
# this check).
_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "walkforward_health_check.py"
)
_spec = importlib.util.spec_from_file_location(
    "walkforward_health_check", _SCRIPT_PATH,
)
assert _spec and _spec.loader, f"cannot load {_SCRIPT_PATH}"
walkforward_health_check = importlib.util.module_from_spec(_spec)
sys.modules["walkforward_health_check"] = walkforward_health_check
_spec.loader.exec_module(walkforward_health_check)


class TestFmtPct:
    """Regression suite for the 2026-05-11 PR #94 formatting bug.

    multi_strategy_backtest writes ``win_rate`` and ``return_pct`` as
    already-scaled percentages (``wins / total * 100``,
    ``total_pnl / cash * 100``). The pre-fix ``_fmt_pct`` multiplied by
    100 again — producing values like "5072%" / "169.3%" in the report.
    """

    def test_already_scaled_percent_not_double_multiplied(self) -> None:
        # 50.72 in the JSON should render as "50.7%", not "5072.0%".
        assert walkforward_health_check._fmt_pct(50.7194) == "50.7%"

    def test_small_return_pct(self) -> None:
        # 3.29% portfolio return → "3.3%", not "329.3%".
        assert walkforward_health_check._fmt_pct(3.2928) == "3.3%"

    def test_negative_return(self) -> None:
        assert walkforward_health_check._fmt_pct(-1.5) == "-1.5%"

    def test_zero(self) -> None:
        assert walkforward_health_check._fmt_pct(0) == "0.0%"

    def test_none(self) -> None:
        assert walkforward_health_check._fmt_pct(None) == "—"


class TestFmt:
    def test_three_decimals(self) -> None:
        assert walkforward_health_check._fmt(1.3374) == "1.337"

    def test_none(self) -> None:
        assert walkforward_health_check._fmt(None) == "—"


# ---------------------------------------------------------------------------
# render_markdown — end-to-end check using a realistic payload
# ---------------------------------------------------------------------------


def _make_payload(
    portfolio_pf_lower: float,
    portfolio_win_rate: float = 50.72,
    portfolio_return_pct: float = 3.29,
) -> dict[str, Any]:
    """Minimal payload shape matching the real walkforward JSON."""
    return {
        "from_date": "2024-05-11",
        "to_date": "2026-05-10",
        "config": {
            "window_days": 90,
            "step_days": 90,
            "bootstrap_samples": 1000,
            "bootstrap_ci": 0.95,
        },
        "aggregate": {
            "_portfolio": {
                "trades": 278,
                "win_rate": portfolio_win_rate,
                "profit_factor": 1.337,
                "return_pct": portfolio_return_pct,
                "sharpe_approx": 0.08,
            },
            "mean_reversion": {
                "trades": 33,
                "win_rate": 24.24,
                "profit_factor": 0.997,
                "return_pct": 0.20,
                "sharpe_approx": -0.001,
            },
        },
        "bootstrap": {
            "_portfolio": {
                "profit_factor": {
                    "lower": portfolio_pf_lower,
                    "upper": 2.0466,
                }
            },
            "mean_reversion": {
                "profit_factor": {"lower": 0.338, "upper": 3.064}
            },
        },
    }


class TestRenderMarkdown:
    def test_win_rate_and_return_render_correctly(self) -> None:
        payload = _make_payload(portfolio_pf_lower=0.88)
        health = walkforward_health_check.extract(payload)
        md = walkforward_health_check.render_markdown(
            payload, health, min_pf_lower=1.0, portfolio_alert=True,
        )
        # The _portfolio row must show "50.7%" / "3.3%", not "5072.0%" / "329.3%".
        assert "50.7%" in md
        assert "3.3%" in md
        assert "5072.0%" not in md
        assert "329.3%" not in md
        # mean_reversion row similarly.
        assert "24.2%" in md
        assert "2424.0%" not in md

    def test_alert_when_lower_bound_below_threshold(self) -> None:
        payload = _make_payload(portfolio_pf_lower=0.88)
        health = walkforward_health_check.extract(payload)
        md = walkforward_health_check.render_markdown(
            payload, health, min_pf_lower=1.0, portfolio_alert=True,
        )
        assert "⚠️" in md
        assert "Portfolio CI degraded" in md

    def test_healthy_when_lower_bound_above_threshold(self) -> None:
        payload = _make_payload(portfolio_pf_lower=1.10)
        health = walkforward_health_check.extract(payload)
        md = walkforward_health_check.render_markdown(
            payload, health, min_pf_lower=1.0, portfolio_alert=False,
        )
        assert "✅" in md
        assert "Portfolio CI healthy" in md


# ---------------------------------------------------------------------------
# Smoke-test against a real walkforward JSON if one exists in
# backtest_results/. Skipped on CI where the file is absent.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not list(Path("backtest_results").glob("walkforward_*.json"))
    if Path("backtest_results").exists()
    else True,
    reason="no walkforward_*.json available",
)
def test_smoke_against_real_walkforward_json() -> None:
    candidates = sorted(Path("backtest_results").glob("walkforward_*.json"))
    payload = json.loads(candidates[-1].read_text())
    health = walkforward_health_check.extract(payload)
    md = walkforward_health_check.render_markdown(
        payload, health, min_pf_lower=1.0, portfolio_alert=False,
    )
    # Sanity: no 4-digit percentages (the bug's fingerprint).
    for line in md.splitlines():
        if "%" not in line:
            continue
        # Look for "NNNN.N%" pattern indicating double-multiplied output.
        import re
        assert not re.search(r"\b\d{4,}\.\d%", line), (
            f"4-digit percent suggests _fmt_pct regression: {line!r}"
        )

#!/usr/bin/env python3
"""Emit signal-postmortem records from closed trades.

Two sources are supported:

* ``--backtest <path>``  — a ``backtest_results/multi_strategy_*.json`` file.
  Every trade in ``strategies[].trades[]`` becomes one postmortem record,
  attributed to that ``strategy_id``.  Useful for retrospective signal
  quality analysis over the full history.
* ``--live``             — closed trades in ``trading_bot/data/trading_bot.db``
  (``trades`` table, rows with a non-null ``exit_price`` and ``exit_time``).
  Attributed to the ``strategy_id`` column.

Records are written to ``reports/postmortems/pm_*.json``; they share schema
with the vendored ``signal-postmortem`` skill so its analyzer can summarise
them without modification:

::

    python3 .claude/skills/signal-postmortem/scripts/postmortem_analyzer.py \\
        --postmortems-dir reports/postmortems/ --summary --output-dir reports/

Outcome categories (``TRUE_POSITIVE``, ``FALSE_POSITIVE``, …) are assigned
by ``classify_outcome`` imported from that same skill, so reclassification
rules stay in one place.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKILL_SCRIPT = (
    PROJECT_ROOT
    / ".claude"
    / "skills"
    / "signal-postmortem"
    / "scripts"
    / "postmortem_recorder.py"
)
REPORTS_DIR = PROJECT_ROOT / "reports" / "postmortems"
DB_PATH = PROJECT_ROOT / "trading_bot" / "data" / "trading_bot.db"

# Classification threshold: returns within ±50bps are NEUTRAL.  Matches the
# skill's default, lifted to a named constant so tuning is obvious.
NEUTRAL_RETURN_THRESHOLD = 0.005


def _load_classifier():
    """Import classify_outcome from the vendored skill (shared source of truth)."""
    if not SKILL_SCRIPT.exists():
        raise FileNotFoundError(f"signal-postmortem skill not found at {SKILL_SCRIPT}")
    spec = importlib.util.spec_from_file_location("signal_postmortem_rec", SKILL_SCRIPT)
    if spec is None or spec.loader is None:
        raise ImportError(f"Unable to load {SKILL_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.classify_outcome


# ---------------------------------------------------------------------------
# Record construction
# ---------------------------------------------------------------------------


def _signal_id(ticker: str, entry_time: str, strategy_id: str) -> str:
    """Deterministic, unique-per-trade signal id."""
    raw = f"{strategy_id}|{ticker}|{entry_time}".encode("utf-8")
    short = hashlib.sha1(raw).hexdigest()[:8]  # nosec B324 - id only, not crypto
    date_part = entry_time[:10] if entry_time else "unknown"
    return f"sig_{ticker.lower()}_{date_part}_{short}"


def _iso_date(ts: str) -> str:
    """Normalise any ISO timestamp to YYYY-MM-DD, else empty string."""
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return ts[:10]


def build_record(
    *,
    ticker: str,
    side: str,
    entry_time: str,
    entry_price: float,
    exit_time: str,
    exit_price: float,
    strategy_id: str,
    classify,
) -> dict[str, Any] | None:
    """Assemble one postmortem record.  Returns ``None`` if inputs are unusable."""
    if entry_price is None or exit_price is None or entry_price <= 0:
        return None
    if not entry_time or not exit_time:
        return None

    predicted_direction = "LONG" if side.upper() == "BUY" else "SHORT"
    signal_date = _iso_date(entry_time)
    exit_date = _iso_date(exit_time)

    # Return from the trader's perspective (LONG gains if price rises).
    raw_return = (exit_price - entry_price) / entry_price
    if predicted_direction == "SHORT":
        raw_return = -raw_return

    holding_days = 0
    try:
        d1 = datetime.fromisoformat(signal_date)
        d2 = datetime.fromisoformat(exit_date)
        holding_days = (d2 - d1).days
    except ValueError:
        pass

    # Use the actual holding period as the sole realized-return bucket.
    bucket = f"{max(holding_days, 1)}d"
    realized_returns = {bucket: round(raw_return, 6)}

    # Regime annotation is unknown here — the existing bot doesn't stamp
    # a regime on each trade.  classify_outcome treats UNKNOWN==UNKNOWN as
    # "no regime shift", which is the correct default.
    outcome = classify(
        predicted_direction,
        raw_return,
        "UNKNOWN",
        "UNKNOWN",
        threshold=NEUTRAL_RETURN_THRESHOLD,
    )

    sig_id = _signal_id(ticker, entry_time, strategy_id)

    return {
        "schema_version": "1.0",
        "postmortem_id": f"pm_{sig_id}",
        "signal_id": sig_id,
        "ticker": ticker,
        "signal_date": signal_date,
        "source_skill": strategy_id,
        "predicted_direction": predicted_direction,
        "entry_price": round(entry_price, 4),
        "realized_returns": realized_returns,
        "exit_price": round(exit_price, 4),
        "exit_date": exit_date,
        "holding_days": holding_days,
        "outcome_category": outcome,
        "regime_at_signal": "UNKNOWN",
        "regime_at_exit": "UNKNOWN",
        "outcome_notes": "",
        "recorded_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------


def iter_backtest_trades(path: Path) -> Iterable[dict[str, Any]]:
    """Yield flat trade dicts from a multi_strategy backtest result."""
    data = json.loads(path.read_text())
    for strategy in data.get("strategies", []) or []:
        strategy_id = strategy.get("strategy_id", "unknown")
        for t in strategy.get("trades", []) or []:
            yield {
                "ticker": t.get("ticker"),
                # Backtester records direction via positive/negative shares;
                # all current strategies are long-only so we default to BUY.
                "side": "BUY" if (t.get("shares") or 0) >= 0 else "SELL",
                "entry_time": t.get("entry_time"),
                "entry_price": t.get("entry_price"),
                "exit_time": t.get("exit_time"),
                "exit_price": t.get("exit_price"),
                "strategy_id": strategy_id,
            }


def iter_live_trades(db_path: Path) -> Iterable[dict[str, Any]]:
    """Yield closed trades from the live SQLite DB."""
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            """
            SELECT ticker, side, entry_time, entry_price,
                   exit_time, exit_price, strategy_id
            FROM trades
            WHERE exit_price IS NOT NULL
              AND exit_time IS NOT NULL
            ORDER BY entry_time
            """
        )
        for row in cur:
            ticker, side, entry_time, entry_price, exit_time, exit_price, strat = row
            yield {
                "ticker": ticker,
                "side": side,
                "entry_time": entry_time,
                "entry_price": entry_price,
                "exit_time": exit_time,
                "exit_price": exit_price,
                # Live trades predate strategy_id being populated; attribute
                # to 'live_unattributed' when NULL so postmortem aggregation
                # still works.
                "strategy_id": strat or "live_unattributed",
            }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--backtest", help="Path to a backtest_results/*.json")
    src.add_argument(
        "--live",
        action="store_true",
        help="Read closed trades from trading_bot.db",
    )
    parser.add_argument(
        "--output-dir",
        default=str(REPORTS_DIR),
        help="Where pm_*.json are written (default: reports/postmortems/)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Cap number of trades processed (0 = no cap)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    classify = _load_classifier()

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.backtest:
        source = Path(args.backtest).resolve()
        if not source.exists():
            print(f"Error: backtest file not found: {source}", file=sys.stderr)
            return 2
        trades = iter_backtest_trades(source)
        label = source.name
    else:
        trades = iter_live_trades(DB_PATH)
        label = f"live:{DB_PATH.name}"

    counts: dict[str, int] = {}
    by_strategy: dict[str, dict[str, int]] = {}
    written = 0

    for idx, t in enumerate(trades):
        if args.limit and idx >= args.limit:
            break
        record = build_record(
            ticker=t.get("ticker") or "",
            side=t.get("side") or "BUY",
            entry_time=t.get("entry_time") or "",
            entry_price=t.get("entry_price") or 0.0,
            exit_time=t.get("exit_time") or "",
            exit_price=t.get("exit_price") or 0.0,
            strategy_id=t.get("strategy_id") or "unknown",
            classify=classify,
        )
        if record is None:
            continue

        out_file = output_dir / f"{record['postmortem_id']}.json"
        out_file.write_text(json.dumps(record, indent=2))
        written += 1

        cat = record["outcome_category"]
        counts[cat] = counts.get(cat, 0) + 1
        strat_bucket = by_strategy.setdefault(
            record["source_skill"], {}
        )
        strat_bucket[cat] = strat_bucket.get(cat, 0) + 1

    print(f"Source     : {label}")
    print(f"Output dir : {output_dir}")
    print(f"Records    : {written}")
    print()
    print("Outcome distribution (overall)")
    print("------------------------------")
    for cat, n in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {cat:<25} {n}")

    print()
    print("Outcome distribution by strategy")
    print("--------------------------------")
    for strat, bucket in sorted(by_strategy.items()):
        total = sum(bucket.values())
        tp = bucket.get("TRUE_POSITIVE", 0)
        fp = bucket.get("FALSE_POSITIVE", 0) + bucket.get("FALSE_POSITIVE_SEVERE", 0)
        neutral = bucket.get("NEUTRAL", 0)
        tp_pct = (tp / total * 100.0) if total else 0.0
        print(
            f"  {strat:<22} n={total:<4}  TP={tp:<4} ({tp_pct:5.1f}%)  "
            f"FP={fp:<4}  NEUTRAL={neutral}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

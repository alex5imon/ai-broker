"""Assemble + render the markdown reconciliation report.

The report groups every classified row by category, prints a per-row
evidence line and a proposed corrective action, and ends with a
bug-hypothesis confirmation table that maps category counts back to the
five bugs in ``docs/self_improve_followups.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from trading_bot.self_improve.reconcile.alpaca_fetch import AlpacaState
from trading_bot.self_improve.reconcile.classify import (
    PositionClass,
    PositionFinding,
    TradeClass,
    TradeFinding,
    classify_position,
    classify_trade,
)
from trading_bot.self_improve.reconcile.db_loaders import _position_lookup


@dataclass(frozen=True)
class ReconcileReport:
    generated_at: datetime
    db_path: str
    account_id: str
    is_paper: bool
    since: datetime
    until: datetime
    strategy_enabled: Mapping[str, bool]
    position_findings: tuple[PositionFinding, ...]
    trade_findings: tuple[TradeFinding, ...]
    raw_alpaca_position_count: int
    raw_alpaca_order_count: int


def build_report(
    *,
    db_path: str,
    state: AlpacaState,
    db_positions: list[dict[str, Any]],
    db_trades: list[dict[str, Any]],
    strategy_enabled: Mapping[str, bool],
    since: datetime,
    until: datetime,
) -> ReconcileReport:
    """Classify every DB row and roll the findings into a single report."""
    position_findings = tuple(
        classify_position(p, state, strategy_enabled) for p in db_positions
    )
    lookup = _position_lookup(db_positions)
    trade_findings = tuple(
        classify_trade(t, lookup) for t in db_trades
    )
    return ReconcileReport(
        generated_at=datetime.now(tz=timezone.utc),
        db_path=db_path,
        account_id=state.account_id,
        is_paper=state.is_paper,
        since=since,
        until=until,
        strategy_enabled=dict(strategy_enabled),
        position_findings=position_findings,
        trade_findings=trade_findings,
        raw_alpaca_position_count=len(state.positions_by_symbol),
        raw_alpaca_order_count=len(state.orders_by_id),
    )


def _counts(items: Iterable[Any], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        label: str = str(getattr(item, key).value)
        out[label] = out.get(label, 0) + 1
    return out


def _fmt_position_row(pf: PositionFinding) -> str:
    p = pf.db_row
    return (
        f"- **id={p.get('id')}** `{p.get('ticker')}` "
        f"strategy=`{p.get('strategy_id') or 'NULL'}` "
        f"qty={p.get('quantity')} status=`{p.get('status')}` "
        f"entry_time=`{p.get('entry_time')}`\n"
        f"  - Evidence: {pf.evidence}\n"
        f"  - Action: {pf.proposed_action}"
    )


def _fmt_trade_row(tf: TradeFinding) -> str:
    t = tf.db_row
    return (
        f"- **id={t.get('id')}** `{t.get('ticker')}` "
        f"strategy=`{t.get('strategy_id') or 'NULL'}` "
        f"qty={t.get('quantity')} entry_time=`{t.get('entry_time')}` "
        f"exit_time=`{t.get('exit_time') or 'NULL'}`\n"
        f"  - Evidence: {tf.evidence}\n"
        f"  - Action: {tf.proposed_action}"
    )


def _render_header(report: ReconcileReport) -> list[str]:
    lines: list[str] = [
        "# Reconciliation report — local DB vs Alpaca",
        "",
        f"_Generated {report.generated_at.isoformat()} — "
        f"account `{report.account_id}` "
        f"({'paper' if report.is_paper else 'live'})_",
        "",
        f"- DB path: `{report.db_path}`",
        f"- Window: `{report.since.date().isoformat()}` to "
        f"`{report.until.date().isoformat()}`",
        f"- Alpaca positions: **{report.raw_alpaca_position_count}** • "
        f"Alpaca orders in window: **{report.raw_alpaca_order_count}**",
        f"- DB rows scanned: positions=**{len(report.position_findings)}**, "
        f"trades=**{len(report.trade_findings)}**",
        "",
    ]
    enabled_str = ", ".join(
        f"`{k}`={'on' if v else 'off'}"
        for k, v in sorted(report.strategy_enabled.items())
    )
    lines.append(f"- Strategy enabled map: {enabled_str or '(none configured)'}")
    lines.append("")
    return lines


def _render_summary_tables(
    pos_counts: Mapping[str, int],
    trade_counts: Mapping[str, int],
) -> list[str]:
    lines: list[str] = ["## Summary", "", "### Positions", ""]
    lines.append("| Classification | Count |")
    lines.append("|---|---|")
    for cls in PositionClass:
        lines.append(f"| {cls.value} | {pos_counts.get(cls.value, 0)} |")
    lines.extend(["", "### Trades", "", "| Classification | Count |", "|---|---|"])
    for cls in TradeClass:
        lines.append(f"| {cls.value} | {trade_counts.get(cls.value, 0)} |")
    lines.append("")
    return lines


def _render_bug_confirmation(
    pos_counts: Mapping[str, int],
    trade_counts: Mapping[str, int],
) -> list[str]:
    return [
        "## Bug-hypothesis confirmation",
        "",
        "Counts above map directly to the data-layer bugs in "
        "[docs/self_improve_followups.md](../docs/self_improve_followups.md):",
        "",
        f"1. **trades.strategy_id NULL on entry** — "
        f"{trade_counts.get(TradeClass.ENTRY_ONLY_PHANTOM.value, 0)} "
        "ENTRY_ONLY_PHANTOM rows. Confirms `_create_position_record` "
        "(order_manager.py:868) inserts trades without strategy_id.",
        f"2. **trades exit UPDATE never matches** — "
        f"{trade_counts.get(TradeClass.MISSING_EXIT.value, 0)} MISSING_EXIT "
        "rows. Confirms `_close_position` (order_manager.py:802) uses "
        "positions.id as the trades WHERE clause.",
        f"3. **Phantom CLOSED on canceled entry** — "
        f"{pos_counts.get(PositionClass.PHANTOM_CLOSE.value, 0)} PHANTOM_CLOSE "
        "rows. Confirms entry-timeout / submit-error paths stamp positions "
        "CLOSED without an actual fill ever existing.",
        f"4. **Orphans on disabled strategies** — "
        f"{pos_counts.get(PositionClass.ORPHAN_DISABLED.value, 0)} "
        f"ORPHAN_DISABLED + "
        f"{pos_counts.get(PositionClass.ORPHAN_UNKNOWN.value, 0)} "
        "ORPHAN_UNKNOWN open rows that no live tick code is managing.",
        f"5. **DB/Alpaca quantity drift** — "
        f"{pos_counts.get(PositionClass.MISMATCH_QTY.value, 0)} MISMATCH_QTY + "
        f"{pos_counts.get(PositionClass.ORPHAN_NOT_HELD.value, 0)} "
        "ORPHAN_NOT_HELD rows where the DB believes one thing and Alpaca "
        "believes another.",
        "",
    ]


def _render_position_findings(
    findings: Iterable[PositionFinding],
) -> list[str]:
    grouped: dict[str, list[PositionFinding]] = {}
    for pf in findings:
        grouped.setdefault(pf.classification.value, []).append(pf)
    lines: list[str] = ["## Position findings", ""]
    for cls in PositionClass:
        bucket = grouped.get(cls.value, [])
        if not bucket:
            continue
        lines.append(f"### {cls.value} ({len(bucket)})")
        lines.append("")
        for pf in bucket:
            lines.append(_fmt_position_row(pf))
        lines.append("")
    return lines


def _render_trade_findings(findings: Iterable[TradeFinding]) -> list[str]:
    grouped: dict[str, list[TradeFinding]] = {}
    for tf in findings:
        grouped.setdefault(tf.classification.value, []).append(tf)
    lines: list[str] = ["## Trade findings", ""]
    for cls in TradeClass:
        bucket = grouped.get(cls.value, [])
        if not bucket:
            continue
        lines.append(f"### {cls.value} ({len(bucket)})")
        lines.append("")
        # COMPLETE rows can run into the hundreds and add no signal — collapse.
        if cls is TradeClass.COMPLETE and len(bucket) > 5:
            lines.append(
                f"_Collapsed: {len(bucket)} fully-populated trade rows. "
                "Listing first 5 only._"
            )
            lines.append("")
            for tf in bucket[:5]:
                lines.append(_fmt_trade_row(tf))
        else:
            for tf in bucket:
                lines.append(_fmt_trade_row(tf))
        lines.append("")
    return lines


def render_markdown(report: ReconcileReport) -> str:
    """Render the report as a markdown document for human review."""
    pos_counts = _counts(report.position_findings, "classification")
    trade_counts = _counts(report.trade_findings, "classification")

    lines: list[str] = []
    lines.extend(_render_header(report))
    lines.extend(_render_summary_tables(pos_counts, trade_counts))
    lines.extend(_render_bug_confirmation(pos_counts, trade_counts))
    lines.extend(_render_position_findings(report.position_findings))
    lines.extend(_render_trade_findings(report.trade_findings))
    lines.extend([
        "---",
        "",
        "_This report is read-only. No DB rows were modified and no orders "
        "were submitted. Use it to plan Phase 2 (DB migration) and Phase 3 "
        "(live order logic fixes)._",
        "",
    ])
    return "\n".join(lines)

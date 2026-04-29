"""DB row loaders and config-derived strategy enabled map.

Read-only: every function takes a ``sqlite3.Connection`` (or a path) and
returns plain dicts; nothing is written.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping


def load_db_positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every row in ``positions`` as a dict, ordered by entry_time."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM positions ORDER BY entry_time"
    ).fetchall()
    return [dict(r) for r in rows]


def load_db_trades(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return every row in ``trades`` as a dict, ordered by entry_time."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trades ORDER BY entry_time"
    ).fetchall()
    return [dict(r) for r in rows]


def _position_lookup(
    positions: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Build ``(ticker, entry_time) -> position row`` for trade pairing."""
    out: dict[tuple[str, str], Mapping[str, Any]] = {}
    for p in positions:
        key = (str(p["ticker"]).upper(), str(p.get("entry_time") or ""))
        out[key] = p
    return out


def load_strategy_enabled_map(config_path: str) -> dict[str, bool]:
    """Return ``{strategy_id: enabled}`` for every strategy defined in config.

    Read directly from ``config.yaml`` so we don't have to instantiate the
    full ``Config`` (which loads a lot of unrelated state and can fail
    reconcile runs in environments where some other config block is mid-
    edit).
    """
    import yaml

    path = Path(config_path).expanduser().resolve()
    with path.open("r", encoding="utf-8") as fh:
        raw: Any = yaml.safe_load(fh)
    multi: Any = (raw or {}).get("multi_strategy") or {}
    strategies: Any = multi.get("strategies") or {}
    out: dict[str, bool] = {}
    if isinstance(strategies, dict):
        for sid, body in strategies.items():
            if isinstance(body, dict):
                out[str(sid)] = bool(body.get("enabled", False))
    return out

"""Safe dictionary lookup that treats None like a missing key.

``dict.get(key, default)`` returns ``default`` only when the key is absent.
If the key is present with a value of ``None`` — as happens when a SQLite
NULL column is converted to a Python dict, or when an alpaca-py model
field is ``None`` — ``dict.get`` returns ``None`` and the default is
silently ignored. This is a trap that has caused real production bugs
(e.g. ``position.get("highest_price", entry_price)`` returning None when
``highest_price`` had never been written, crashing downstream arithmetic).

Use :func:`coalesce` at every boundary where a dict is constructed from
a DB row or an external SDK's model, and the default matters.
"""

from __future__ import annotations

from typing import Any


def coalesce(d: dict[str, Any], key: str, default: Any) -> Any:
    """Return ``d[key]`` if present and not None, else ``default``.

    Unlike ``d.get(key, default)``, a present-but-None value is treated
    as missing. This is the correct semantic for dicts built from DB
    rows where NULL becomes Python None.
    """
    value = d.get(key)
    if value is None:
        return default
    return value

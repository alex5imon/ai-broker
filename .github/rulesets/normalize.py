"""Normalize a GitHub repository ruleset payload to its policy invariants.

Reads the ruleset JSON on stdin (either the list endpoint's array or a single
GET-by-id object) and writes a stable, normalized JSON document on stdout
(``sort_keys=True``, 2-space indent) so two normalizations of the same policy
compare byte-for-byte.

Volatile / environment-specific fields are dropped: ``id``, ``node_id``,
``source``, ``source_type``, ``_links``, ``created_at``, ``updated_at``,
``current_user_can_bypass``. Only the policy invariants are kept.

Used by BOTH the committed snapshot at ``.github/rulesets/main.json`` and
``.github/workflows/ruleset-drift.yml``. Regenerate the snapshot with:

    gh api /repos/<owner>/<repo>/rulesets/<id> \
        | python3 .github/rulesets/normalize.py > .github/rulesets/main.json
"""

from __future__ import annotations

import json
import sys
from typing import Any

# Policy fields that define the branch-protection contract. Everything else in
# the ruleset payload is volatile state, not policy.
_KEEP: tuple[str, ...] = (
    "name",
    "target",
    "enforcement",
    "conditions",
    "bypass_actors",
)

_RULESET_NAME: str = "master"


def normalize(ruleset: dict[str, Any]) -> dict[str, Any]:
    """Project a ruleset down to its policy invariants, rules sorted by type."""
    out: dict[str, Any] = {key: ruleset.get(key) for key in _KEEP}
    rules: list[dict[str, Any]] = list(ruleset.get("rules") or [])
    out["rules"] = sorted(rules, key=lambda rule: rule.get("type", ""))
    return out


def main() -> int:
    data: Any = json.load(sys.stdin)
    # The list endpoint returns an array of summaries; GET-by-id returns one
    # object. Accept either so callers can pipe whichever they fetched.
    if isinstance(data, list):
        data = next(
            (rs for rs in data if rs.get("name") == _RULESET_NAME),
            {},
        )
    print(json.dumps(normalize(data), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

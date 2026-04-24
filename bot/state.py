from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class State:
    last_bar_iso: str | None = None
    strategy: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        return cls(
            last_bar_iso=data.get("last_bar_iso"),
            strategy=data.get("strategy", {}),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"last_bar_iso": self.last_bar_iso, "strategy": self.strategy},
                indent=2,
                sort_keys=True,
            )
        )

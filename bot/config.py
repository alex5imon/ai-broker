from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    env: str
    api_key_id: str
    api_secret: str
    log_dir: Path
    state_dir: Path

    @property
    def is_paper(self) -> bool:
        return self.env == "paper"


def load_config() -> Config:
    load_dotenv()

    env = os.environ.get("ALPACA_ENV", "paper").lower()
    if env not in {"paper", "live"}:
        raise ValueError(f"ALPACA_ENV must be 'paper' or 'live', got {env!r}")

    key = os.environ.get("ALPACA_API_KEY_ID", "")
    secret = os.environ.get("ALPACA_API_SECRET", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY_ID and ALPACA_API_SECRET must be set")

    log_dir = Path(os.environ.get("BOT_LOG_DIR", "logs"))
    state_dir = Path(os.environ.get("BOT_STATE_DIR", "state"))
    log_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        env=env,
        api_key_id=key,
        api_secret=secret,
        log_dir=log_dir,
        state_dir=state_dir,
    )

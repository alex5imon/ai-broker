from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path


class JsonlFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extras = getattr(record, "extras", None)
        if isinstance(extras, dict):
            payload.update(extras)
        return json.dumps(payload, sort_keys=True)


def setup_logging(log_dir: Path, run_tag: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bot")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_dir / f"run-{run_tag}.jsonl")
    fh.setFormatter(JsonlFormatter())
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(JsonlFormatter())
    logger.addHandler(sh)
    return logger


def log_event(logger: logging.Logger, event: str, **extras: object) -> None:
    logger.info(event, extra={"extras": {"event": event, **extras}})

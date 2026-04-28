"""Per-strategy cooldown after N consecutive losses.

Records each closed trade outcome for a strategy. After the configured
number of consecutive losses, the strategy is paused for a configured
cooldown window. A profitable trade resets the counter and lifts the
cooldown immediately.

State is persisted in ``risk_circuit_state`` (key=``loss_cooldown:<sid>``)
so it survives across stateless cron ticks. The state blob carries
``consecutive_losses`` and ``cooldown_until`` (ISO-8601, US/Eastern).
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trading_bot.constants import TZ_EASTERN
from trading_bot.db import repository as repo

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


def _key(strategy_id: str) -> str:
    return f"loss_cooldown:{strategy_id}"


@dataclass(frozen=True)
class LossCooldownConfig:
    """Configuration values needed by :class:`LossCooldownTracker`."""

    enabled: bool = False
    threshold_losses: int = 3
    cooldown_minutes: int = 240


class LossCooldownTracker:
    """Tracks consecutive losses per strategy and enforces a cooldown."""

    def __init__(self, db_path: str, config: LossCooldownConfig) -> None:
        self._db_path: str = db_path
        self._config: LossCooldownConfig = config

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    def record_outcome(self, strategy_id: str, pnl: float) -> None:
        """Record a closed-trade outcome for *strategy_id*.

        ``pnl > 0`` resets the counter and clears any active cooldown.
        ``pnl <= 0`` increments the counter; if it reaches the configured
        threshold, a cooldown window is set.
        """
        if not self._config.enabled:
            return

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                state = repo.load_risk_state(conn, _key(strategy_id))
                consecutive: int = 0
                if state is not None:
                    consecutive = int((state.get("state") or {}).get("consecutive_losses", 0))

                if pnl > 0:
                    if consecutive == 0 and (state is None or not state.get("tripped")):
                        return
                    repo.save_risk_state(
                        conn, _key(strategy_id),
                        tripped=False,
                        reason=None,
                        state={"consecutive_losses": 0, "cooldown_until": None},
                    )
                    if consecutive > 0:
                        logger.info(
                            "[%s] Loss cooldown reset after winning trade "
                            "(prior streak=%d)",
                            strategy_id, consecutive,
                        )
                    return

                consecutive += 1
                if consecutive < self._config.threshold_losses:
                    repo.save_risk_state(
                        conn, _key(strategy_id),
                        tripped=False,
                        reason=None,
                        state={
                            "consecutive_losses": consecutive,
                            "cooldown_until": None,
                        },
                    )
                    return

                cooldown_until: datetime = datetime.now(tz=ET) + timedelta(
                    minutes=self._config.cooldown_minutes,
                )
                repo.save_risk_state(
                    conn, _key(strategy_id),
                    tripped=True,
                    reason=(
                        f"{consecutive} consecutive losses — cooldown until "
                        f"{cooldown_until.strftime('%Y-%m-%d %H:%M ET')}"
                    ),
                    state={
                        "consecutive_losses": consecutive,
                        "cooldown_until": cooldown_until.isoformat(),
                    },
                )
                logger.warning(
                    "[%s] Loss cooldown engaged: %d consecutive losses, "
                    "paused until %s",
                    strategy_id, consecutive,
                    cooldown_until.strftime("%Y-%m-%d %H:%M ET"),
                )
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "Failed to record loss-cooldown outcome for %s", strategy_id,
            )

    def is_on_cooldown(self, strategy_id: str) -> tuple[bool, str | None]:
        """Return ``(active, reason)`` for *strategy_id*'s cooldown state."""
        if not self._config.enabled:
            return False, None
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                state = repo.load_risk_state(conn, _key(strategy_id))
            finally:
                conn.close()
        except Exception:
            logger.warning(
                "Failed to load loss-cooldown state for %s", strategy_id, exc_info=True,
            )
            return False, None

        if state is None or not state.get("tripped"):
            return False, None

        cooldown_until_iso: str | None = (state.get("state") or {}).get("cooldown_until")
        if not cooldown_until_iso:
            return False, None

        try:
            cooldown_until: datetime = datetime.fromisoformat(cooldown_until_iso)
        except ValueError:
            return False, None
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=ET)

        now: datetime = datetime.now(tz=ET)
        if now >= cooldown_until:
            self._auto_clear(strategy_id)
            return False, None

        reason: str = state.get("reason") or "loss cooldown active"
        return True, reason

    def _auto_clear(self, strategy_id: str) -> None:
        """Clear an expired cooldown but preserve the consecutive-loss count."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                state = repo.load_risk_state(conn, _key(strategy_id))
                consecutive: int = 0
                if state is not None:
                    consecutive = int((state.get("state") or {}).get("consecutive_losses", 0))
                repo.save_risk_state(
                    conn, _key(strategy_id),
                    tripped=False,
                    reason=None,
                    state={
                        "consecutive_losses": consecutive,
                        "cooldown_until": None,
                    },
                )
                logger.info(
                    "[%s] Loss cooldown expired — entries re-enabled "
                    "(streak=%d preserved)",
                    strategy_id, consecutive,
                )
            finally:
                conn.close()
        except Exception:
            logger.exception(
                "Failed to auto-clear loss-cooldown for %s", strategy_id,
            )

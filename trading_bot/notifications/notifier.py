"""Push notification system via ntfy.sh with rate limiting and fallback.

Refactored to be synchronous under the hood for the tick-model bot (Phase 2).
The public methods retain their ``async def`` signatures so existing callers
(``await notifier.send(...)``) keep working; the bodies perform a blocking
HTTP POST via ``requests`` and return immediately.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from typing import Any

import requests

logger: logging.Logger = logging.getLogger(__name__)


class Notifier:
    """Send push notifications via ntfy.sh with rate limiting and macOS fallback.

    Configuration is read from the ``notifications`` section of config.yaml.
    Rate limiting enforces a maximum of ``rate_limit_per_minute`` sends per
    rolling 60-second window.  Notifications that exceed the rate limit are
    dropped (with a log line) rather than queued — the stateless tick model
    has no long-running drain loop.  If ntfy.sh is unreachable after
    ``max_retries`` consecutive failures the notifier falls back to macOS
    ``osascript`` display-notification until connectivity is restored.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        ntfy_cfg: dict[str, Any] = config.get("notifications", {})
        self._server: str = ntfy_cfg.get("ntfy_server", "https://ntfy.sh")
        self._topic: str = ntfy_cfg.get("ntfy_topic", "REDACTED_TOPIC")
        self._kill_topic: str = ntfy_cfg.get(
            "ntfy_kill_topic", "REDACTED_KILL_TOPIC"
        )
        self._max_retries: int = int(ntfy_cfg.get("max_retries", 3))
        self._retry_interval: float = float(
            ntfy_cfg.get("retry_interval_seconds", 60)
        )
        self._rate_limit: int = int(ntfy_cfg.get("rate_limit_per_minute", 5))
        self._fallback_osascript: bool = bool(
            ntfy_cfg.get("fallback_to_osascript", True)
        )

        self._priorities: dict[str, int] = {
            k: int(v) for k, v in ntfy_cfg.get("priorities", {}).items()
        }

        self._send_timestamps: deque[float] = deque()
        self._consecutive_failures: int = 0
        self._ntfy_available: bool = True

        self._session: requests.Session = requests.Session()

    # ------------------------------------------------------------------
    # Lifecycle — kept as no-op coroutines for backwards compat with
    # the current async caller in main.py.  They will be removed once
    # main.py is rewritten as a stateless tick in a follow-up commit.
    # ------------------------------------------------------------------

    async def start(self) -> None:
        logger.debug(
            "Notifier ready (server=%s, topic=%s, rate_limit=%d/min)",
            self._server,
            self._topic,
            self._rate_limit,
        )

    async def shutdown(self) -> None:
        self._session.close()
        logger.debug("Notifier session closed")

    # ------------------------------------------------------------------
    # Public send API
    # ------------------------------------------------------------------

    async def send(
        self,
        title: str,
        message: str,
        priority: int = 3,
        tags: list[str] | None = None,
    ) -> None:
        """Send a notification, respecting the per-minute rate limit.

        If the rate limit has been reached the notification is dropped (with a
        log line).  There is no background drain loop in the tick model.
        """
        resolved_tags: list[str] = tags if tags is not None else []

        if not self._can_send_now():
            logger.warning(
                "Rate limit (%d/min) hit — dropping notification: %s",
                self._rate_limit,
                title,
            )
            return

        self._do_send(title, message, priority, resolved_tags)

    # ------------------------------------------------------------------
    # Convenience methods for specific events
    # ------------------------------------------------------------------

    async def trade_entry(
        self,
        ticker: str,
        side: str,
        price: float,
        qty: int,
        reason: str,
    ) -> None:
        title: str = f"\U0001f4c8 [TRADE ENTRY] {ticker}"
        message: str = (
            f"Side: {side}\n"
            f"Price: {price}\n"
            f"Qty: {qty}\n"
            f"Reason: {reason}"
        )
        priority: int = self._priorities.get("trade_entry", 3)
        await self.send(title, message, priority, tags=["chart_with_upwards_trend"])

    async def position_closed(
        self,
        ticker: str,
        pnl: float,
        hold_time: str,
        exit_reason: str,
    ) -> None:
        emoji: str = "\u2705" if pnl >= 0 else "\u274c"
        title: str = f"{emoji} [POSITION CLOSED] {ticker}"
        # P&L is denominated in USD \u2014 Alpaca trades execute in USD and there
        # is no FX conversion in this notification path.
        message: str = (
            f"P&L: ${pnl:+.2f}\n"
            f"Hold time: {hold_time}\n"
            f"Exit reason: {exit_reason}"
        )
        priority: int = self._priorities.get("position_closed", 3)
        await self.send(title, message, priority, tags=["moneybag"])

    async def stop_loss_hit(self, ticker: str, loss: float) -> None:
        title: str = f"\U0001f6d1 [STOP LOSS] {ticker}"
        message: str = f"Loss: ${loss:+.2f}"
        priority: int = self._priorities.get("stop_loss_hit", 4)
        await self.send(title, message, priority, tags=["rotating_light"])

    async def daily_summary(
        self,
        trades: int,
        net_pnl: float,
        win_rate: float,
    ) -> None:
        title: str = "\U0001f4ca [DAILY SUMMARY]"
        message: str = (
            f"Trades: {trades}\n"
            f"Net P&L: {net_pnl:+.2f}\n"
            f"Win rate: {win_rate:.0%}"
        )
        priority: int = self._priorities.get("daily_summary", 3)
        await self.send(title, message, priority, tags=["bar_chart"])

    async def phase0_cleanup(self, plan: str) -> None:
        title: str = "\U0001f9f9 [PHASE 0] Cleanup Plan"
        priority: int = self._priorities.get("phase0_cleanup", 4)
        await self.send(title, plan, priority, tags=["broom"])

    async def phase_transition(
        self,
        from_phase: int,
        to_phase: int,
        equity: float,
    ) -> None:
        direction: str = "upgrade" if to_phase > from_phase else "DEMOTION"
        emoji: str = "\U0001f680" if to_phase > from_phase else "\u26a0\ufe0f"
        title: str = f"{emoji} [PHASE {direction.upper()}] {from_phase} -> {to_phase}"
        message: str = f"Equity: \u00a3{equity:,.2f}"
        priority: int = self._priorities.get("phase_transition", 4)
        await self.send(
            title,
            message,
            priority,
            tags=["rocket" if to_phase > from_phase else "warning"],
        )

    async def gateway_alert(
        self,
        message: str,
        is_critical: bool = False,
    ) -> None:
        if is_critical:
            title: str = "\U0001f6a8 [GATEWAY] CRITICAL"
            priority: int = self._priorities.get("gateway_disconnect", 5)
            tags: list[str] = ["rotating_light"]
        else:
            title = "\U0001f310 [GATEWAY] Reconnected"
            priority = self._priorities.get("gateway_reconnect", 3)
            tags = ["globe_with_meridians"]
        await self.send(title, message, priority, tags=tags)

    async def kill_switch(self, message: str) -> None:
        title: str = "\u2620\ufe0f [KILL SWITCH] Activated"
        priority: int = self._priorities.get("kill_switch", 5)
        await self.send(title, message, priority, tags=["skull_and_crossbones"])

    async def drawdown_alert(
        self,
        drawdown_pct: float,
        pause_days: int,
    ) -> None:
        title: str = "\U0001f4c9 [DRAWDOWN BREAKER] Triggered"
        message: str = (
            f"Drawdown: {drawdown_pct:.1%} from 5-day peak\n"
            f"Trading paused for {pause_days} day(s)"
        )
        priority: int = self._priorities.get("drawdown_breaker", 5)
        await self.send(title, message, priority, tags=["chart_with_downwards_trend"])

    async def bot_startup(
        self,
        phase: int,
        positions: int,
        mode: str = "live",
    ) -> None:
        title: str = "\u25b6\ufe0f [BOT] Started"
        message: str = (
            f"Mode: {mode}\n"
            f"Phase: {phase}\n"
            f"Open positions: {positions}"
        )
        priority: int = self._priorities.get("bot_startup", 2)
        await self.send(title, message, priority, tags=["arrow_forward"])

    async def bot_shutdown(self, daily_pnl: float) -> None:
        title: str = "\u23f9\ufe0f [BOT] Shutdown"
        message: str = f"Daily P&L: {daily_pnl:+.2f}"
        priority: int = self._priorities.get("bot_shutdown", 2)
        await self.send(title, message, priority, tags=["stop_button"])

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _can_send_now(self) -> bool:
        """Return True if we are within the rate limit window."""
        now: float = time.monotonic()
        while self._send_timestamps and (now - self._send_timestamps[0] > 60.0):
            self._send_timestamps.popleft()
        return len(self._send_timestamps) < self._rate_limit

    def _do_send(
        self,
        title: str,
        message: str,
        priority: int,
        tags: list[str],
    ) -> bool:
        """POST to ntfy.sh synchronously.  Returns True on success."""
        url: str = f"{self._server}/{self._topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": str(max(1, min(5, priority))),
        }
        if tags:
            headers["Tags"] = ",".join(tags)

        for attempt in range(1, self._max_retries + 1):
            try:
                resp = self._session.post(
                    url,
                    data=message.encode("utf-8"),
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code < 300:
                    self._send_timestamps.append(time.monotonic())
                    self._consecutive_failures = 0
                    if not self._ntfy_available:
                        self._ntfy_available = True
                        logger.info("ntfy.sh connectivity restored")
                    return True
                logger.warning(
                    "ntfy.sh returned HTTP %d on attempt %d/%d",
                    resp.status_code,
                    attempt,
                    self._max_retries,
                )
            except Exception:
                logger.warning(
                    "ntfy.sh POST failed (attempt %d/%d)",
                    attempt,
                    self._max_retries,
                    exc_info=True,
                )

            if attempt < self._max_retries:
                # Bounded sleep — tick must finish quickly, so cap inter-retry
                # wait at a few seconds regardless of configured interval.
                time.sleep(min(self._retry_interval, 2.0))

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_retries and self._ntfy_available:
            self._ntfy_available = False
            logger.warning(
                "ntfy.sh unreachable after %d consecutive failures; "
                "falling back to osascript",
                self._consecutive_failures,
            )

        if self._fallback_osascript:
            self._send_osascript(title, message)

        return False

    @staticmethod
    def _osa_safe(value: str) -> str:
        """Strip everything an AppleScript string literal can interpret.

        AppleScript's quoted-string syntax interprets ``\\``, ``"``, backticks,
        and ``$`` (when the calling context is a shell, not osascript itself,
        but we still want to be conservative).  Notification text is
        bot-generated today, but anything that ever flows in from external
        sources (Alpaca error strings echoing tickers, news headlines, etc.)
        could otherwise smuggle in characters that break out of the literal.
        """
        cleaned: str = value.encode("ascii", "ignore").decode("ascii")
        # Drop any byte that could carry meaning inside the AppleScript
        # source we're about to assemble.  Whitespace is collapsed to a
        # single space so multi-line messages still render legibly.
        return "".join(
            ch if (ch.isprintable() and ch not in '"\\`$') else " "
            for ch in cleaned
        ).strip()

    def _send_osascript(self, title: str, message: str) -> None:
        """Best-effort macOS native notification fallback."""
        clean_title: str = self._osa_safe(title)
        clean_msg: str = self._osa_safe(message)
        script: str = (
            f'display notification "{clean_msg}" '
            f'with title "{clean_title}"'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                timeout=5,
                check=False,
            )
            logger.debug("Sent osascript fallback notification: %s", clean_title)
        except Exception:
            logger.debug("osascript fallback also failed", exc_info=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def kill_topic(self) -> str:
        return self._kill_topic

    @property
    def is_ntfy_available(self) -> bool:
        return self._ntfy_available

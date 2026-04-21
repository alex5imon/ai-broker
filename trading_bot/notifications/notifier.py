"""Push notification system via ntfy.sh with rate limiting and fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import ssl

import aiohttp
import certifi

logger: logging.Logger = logging.getLogger(__name__)


@dataclass
class _QueuedNotification:
    """A notification waiting to be sent."""

    title: str
    message: str
    priority: int
    tags: list[str]
    timestamp: float = field(default_factory=time.monotonic)


class Notifier:
    """Send push notifications via ntfy.sh with rate limiting and macOS fallback.

    Configuration is read from the ``notifications`` section of config.yaml.
    Rate limiting enforces a maximum of ``rate_limit_per_minute`` sends per
    rolling 60-second window.  Excess notifications are queued and drained
    automatically.  If ntfy.sh is unreachable after ``max_retries`` consecutive
    failures the notifier falls back to macOS ``osascript`` display-notification
    until connectivity is restored.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

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

        # Priority map for convenience methods
        self._priorities: dict[str, int] = {
            k: int(v) for k, v in ntfy_cfg.get("priorities", {}).items()
        }

        # Rate-limiting state
        self._send_timestamps: deque[float] = deque()
        self._queue: asyncio.Queue[_QueuedNotification] = asyncio.Queue()
        self._drain_task: asyncio.Task[None] | None = None

        # Connectivity tracking
        self._consecutive_failures: int = 0
        self._ntfy_available: bool = True

        # Shared HTTP session (created lazily)
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background queue-drain task."""
        self._session = aiohttp.ClientSession()
        self._drain_task = asyncio.create_task(
            self._drain_loop(), name="notifier-drain"
        )
        logger.info(
            "Notifier started (server=%s, topic=%s, rate_limit=%d/min)",
            self._server,
            self._topic,
            self._rate_limit,
        )

    async def shutdown(self) -> None:
        """Flush remaining notifications and close the HTTP session."""
        if self._drain_task is not None:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        # Best-effort flush of anything still queued
        while not self._queue.empty():
            item: _QueuedNotification = self._queue.get_nowait()
            await self._do_send(item.title, item.message, item.priority, item.tags)

        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

        logger.info("Notifier shut down")

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

        If the rate limit has been reached the notification is enqueued and
        will be dispatched by the background drain loop.
        """
        resolved_tags: list[str] = tags if tags is not None else []

        if self._can_send_now():
            await self._do_send(title, message, priority, resolved_tags)
        else:
            logger.debug("Rate limit hit - queuing notification: %s", title)
            await self._queue.put(
                _QueuedNotification(
                    title=title,
                    message=message,
                    priority=priority,
                    tags=resolved_tags,
                )
            )

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
        """Notify about a new trade entry."""
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
        """Notify about a closed position."""
        emoji: str = "\u2705" if pnl >= 0 else "\u274c"
        title: str = f"{emoji} [POSITION CLOSED] {ticker}"
        message: str = (
            f"P&L: {pnl:+.2f}\n"
            f"Hold time: {hold_time}\n"
            f"Exit reason: {exit_reason}"
        )
        priority: int = self._priorities.get("position_closed", 3)
        await self.send(title, message, priority, tags=["moneybag"])

    async def stop_loss_hit(self, ticker: str, loss: float) -> None:
        """Notify about a stop-loss fill."""
        title: str = f"\U0001f6d1 [STOP LOSS] {ticker}"
        message: str = f"Loss: {loss:+.2f}"
        priority: int = self._priorities.get("stop_loss_hit", 4)
        await self.send(title, message, priority, tags=["rotating_light"])

    async def daily_summary(
        self,
        trades: int,
        net_pnl: float,
        win_rate: float,
    ) -> None:
        """Send end-of-day summary notification."""
        title: str = "\U0001f4ca [DAILY SUMMARY]"
        message: str = (
            f"Trades: {trades}\n"
            f"Net P&L: {net_pnl:+.2f}\n"
            f"Win rate: {win_rate:.0%}"
        )
        priority: int = self._priorities.get("daily_summary", 3)
        await self.send(title, message, priority, tags=["bar_chart"])

    async def phase0_cleanup(self, plan: str) -> None:
        """Notify about Phase 0 cleanup plan (sent before execution)."""
        title: str = "\U0001f9f9 [PHASE 0] Cleanup Plan"
        message: str = plan
        priority: int = self._priorities.get("phase0_cleanup", 4)
        await self.send(title, message, priority, tags=["broom"])

    async def phase_transition(
        self,
        from_phase: int,
        to_phase: int,
        equity: float,
    ) -> None:
        """Notify about a phase transition."""
        direction: str = "upgrade" if to_phase > from_phase else "DEMOTION"
        emoji: str = "\U0001f680" if to_phase > from_phase else "\u26a0\ufe0f"
        title: str = f"{emoji} [PHASE {direction.upper()}] {from_phase} -> {to_phase}"
        message: str = f"Equity: \u00a3{equity:,.2f}"
        priority: int = self._priorities.get("phase_transition", 4)
        await self.send(title, message, priority, tags=["rocket" if to_phase > from_phase else "warning"])

    async def gateway_alert(
        self,
        message: str,
        is_critical: bool = False,
    ) -> None:
        """Notify about gateway connection events."""
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
        """Notify about kill-switch activation."""
        title: str = "\u2620\ufe0f [KILL SWITCH] Activated"
        priority: int = self._priorities.get("kill_switch", 5)
        await self.send(title, message, priority, tags=["skull_and_crossbones"])

    async def drawdown_alert(
        self,
        drawdown_pct: float,
        pause_days: int,
    ) -> None:
        """Notify about drawdown circuit breaker activation."""
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
        """Notify about bot startup."""
        title: str = "\u25b6\ufe0f [BOT] Started"
        message: str = (
            f"Mode: {mode}\n"
            f"Phase: {phase}\n"
            f"Open positions: {positions}"
        )
        priority: int = self._priorities.get("bot_startup", 2)
        await self.send(title, message, priority, tags=["arrow_forward"])

    async def bot_shutdown(self, daily_pnl: float) -> None:
        """Notify about bot shutdown."""
        title: str = "\u23f9\ufe0f [BOT] Shutdown"
        message: str = f"Daily P&L: {daily_pnl:+.2f}"
        priority: int = self._priorities.get("bot_shutdown", 2)
        await self.send(title, message, priority, tags=["stop_button"])

    # ------------------------------------------------------------------
    # Kill-switch listener
    # ------------------------------------------------------------------

    async def listen_kill_switch(
        self,
        callback: Any,  # Callable[[], Awaitable[None]]
    ) -> None:
        """Subscribe to the kill-switch ntfy topic via SSE.

        When a message arrives on the kill topic, *callback* is awaited.
        This coroutine runs indefinitely and should be launched as a task.
        """
        url: str = f"{self._server}/{self._kill_topic}/sse"
        logger.info("Listening for kill switch on %s", url)

        while True:
            try:
                session: aiohttp.ClientSession = await self._ensure_session()
                async with session.get(url, timeout=None) as resp:
                    async for line_bytes in resp.content:
                        line: str = line_bytes.decode("utf-8", errors="replace").strip()
                        if not line.startswith("data:"):
                            continue
                        payload: str = line[5:].strip()
                        if not payload:
                            continue

                        # ntfy SSE emits several JSON frame types. Only act on
                        # "message" events — "open"/"keepalive"/"poll_request"
                        # are transport housekeeping and must not trigger halts.
                        try:
                            frame: dict[str, Any] = json.loads(payload)
                        except json.JSONDecodeError:
                            logger.debug("Non-JSON SSE frame ignored: %s", payload)
                            continue

                        event: str = str(frame.get("event", ""))
                        if event != "message":
                            logger.debug("Ignoring ntfy SSE event=%s", event)
                            continue

                        msg_body: str = str(frame.get("message", "")).strip()
                        logger.warning(
                            "Kill switch message received: %r", msg_body or "(empty)",
                        )
                        await callback()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kill switch listener error, retrying in 30s")
                await asyncio.sleep(30)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            ssl_ctx: ssl.SSLContext = ssl.create_default_context(
                cafile=certifi.where()
            )
            connector: aiohttp.TCPConnector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    def _can_send_now(self) -> bool:
        """Return True if we are within the rate limit window."""
        now: float = time.monotonic()
        # Purge timestamps older than 60 seconds
        while self._send_timestamps and (now - self._send_timestamps[0] > 60.0):
            self._send_timestamps.popleft()
        return len(self._send_timestamps) < self._rate_limit

    async def _do_send(
        self,
        title: str,
        message: str,
        priority: int,
        tags: list[str],
    ) -> bool:
        """Attempt to POST to ntfy.sh.  Returns True on success."""
        url: str = f"{self._server}/{self._topic}"
        headers: dict[str, str] = {
            "Title": title,
            "Priority": str(max(1, min(5, priority))),
        }
        if tags:
            headers["Tags"] = ",".join(tags)

        for attempt in range(1, self._max_retries + 1):
            try:
                session: aiohttp.ClientSession = await self._ensure_session()
                async with session.post(
                    url,
                    data=message.encode("utf-8"),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status < 300:
                        self._send_timestamps.append(time.monotonic())
                        self._consecutive_failures = 0
                        if not self._ntfy_available:
                            self._ntfy_available = True
                            logger.info("ntfy.sh connectivity restored")
                        return True
                    logger.warning(
                        "ntfy.sh returned HTTP %d on attempt %d/%d",
                        resp.status,
                        attempt,
                        self._max_retries,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "ntfy.sh POST failed (attempt %d/%d)",
                    attempt,
                    self._max_retries,
                    exc_info=True,
                )

            if attempt < self._max_retries:
                await asyncio.sleep(self._retry_interval)

        # All retries exhausted
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

    def _send_osascript(self, title: str, message: str) -> None:
        """Best-effort macOS native notification fallback."""
        # Strip emoji for osascript (it handles them, but keep it clean)
        clean_title: str = title.encode("ascii", "ignore").decode("ascii").strip()
        clean_msg: str = message.replace('"', '\\"').replace("\n", " | ")
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
            # Benign: osascript is a best-effort local fallback for ntfy.sh.
            logger.debug("osascript fallback also failed", exc_info=True)

    async def _drain_loop(self) -> None:
        """Background loop that drains queued notifications when rate allows."""
        try:
            while True:
                item: _QueuedNotification = await self._queue.get()
                # Wait until rate limit permits
                while not self._can_send_now():
                    await asyncio.sleep(1.0)
                await self._do_send(item.title, item.message, item.priority, item.tags)
                self._queue.task_done()
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def kill_topic(self) -> str:
        """The ntfy topic used for kill-switch commands."""
        return self._kill_topic

    @property
    def is_ntfy_available(self) -> bool:
        """Whether ntfy.sh is currently considered reachable."""
        return self._ntfy_available

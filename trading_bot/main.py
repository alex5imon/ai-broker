"""Main trading bot orchestrator — stateless GHA tick entrypoint.

Each invocation runs a single ``tick()``: connect, reconcile state, refresh
quotes, evaluate entries/exits for the current market window, and exit.  The
per-tick business logic (pre-market scan, entry scan, exit check, wind-down,
phase transition, daily summary) is persisted via the SQLite ``tick_state`` +
``risk_circuit_state`` tables so the next tick resumes where this one left off.

Usage::

    python -m trading_bot.main [--config CONFIG] [--mode {premarket,normal,close-only}] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import logging.handlers
import sqlite3
from datetime import date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading_bot.config import Config
from trading_bot.constants import (
    GICS_SECTOR,
    HoldType,
    Market,
    Phase,
    PositionStatus,
    TZ_EASTERN,
    TZ_UTC,
)
from trading_bot.data.earnings import EarningsCalendar
from trading_bot.data.fx import FXManager
from trading_bot.data.market_data import MarketDataManager
from trading_bot.data.sentiment import SentimentAnalyzer
from trading_bot.db import repository as repo
from trading_bot.db.migrations import run_migrations
from trading_bot.execution.loss_cooldown import LossCooldownConfig, LossCooldownTracker
from trading_bot.execution.order_manager import EntryDecision as OMEntryDecision
from trading_bot.execution.order_manager import OrderManager
from trading_bot.execution.risk_manager import RiskManager
from trading_bot.execution.settlement_tracker import SettlementTracker
from trading_bot.gateway.connection import GatewayConnection
from trading_bot.gateway.recovery import StateRecovery
from trading_bot.notifications.notifier import Notifier
from trading_bot.execution.virtual_portfolio import PortfolioManager
from trading_bot.reporting.daily_report import ReportGenerator
from trading_bot.reporting.performance import PerformanceCalculator
from trading_bot.strategy.entry import EntryDecision, EntryEvaluator
from trading_bot.strategy.strategies import create_strategies
from trading_bot.strategy.strategy_manager import StrategyManager
from trading_bot.strategy.regime_filter import RegimeFilter
from trading_bot.strategy.exit import ExitManager
from trading_bot.strategy.technical import TechnicalAnalyzer

logger: logging.Logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Timezone aliases
# ---------------------------------------------------------------------------

_EASTERN: ZoneInfo = TZ_EASTERN

_MARKET_TZ: dict[Market, ZoneInfo] = {
    Market.US: _EASTERN,
}


# ---------------------------------------------------------------------------
# TradingBot
# ---------------------------------------------------------------------------


class TradingBot:
    """Main trading bot orchestrator - ties all modules together."""

    def __init__(
        self,
        config: Config,
        mode: str = "normal",
        dry_run: bool = False,
    ) -> None:
        self._config: Config = config
        self._mode: str = mode  # "premarket" | "normal" | "close-only"
        self._dry_run: bool = dry_run

        raw: dict[str, Any] = config._raw  # raw dict for modules that accept it
        db_path: str = config.db_path
        self._db_path: str = db_path

        # Apply schema migrations up-front — components constructed below
        # (PortfolioManager, RiskManager, etc.) read/write tables in their
        # constructors, so the DB must be at the target version before we
        # instantiate anything that touches it.
        run_migrations(db_path)

        # --- Core infrastructure ---
        self._notifier: Notifier = Notifier(raw)
        self._gateway: GatewayConnection = GatewayConnection(raw, self._notifier)

        # --- Data layer ---
        self._fx: FXManager = FXManager(self._gateway, raw)
        self._market_data: MarketDataManager = MarketDataManager(
            self._gateway, raw, self._notifier
        )
        self._sentiment: SentimentAnalyzer = SentimentAnalyzer(raw, db_path)
        self._earnings: EarningsCalendar = EarningsCalendar(raw, db_path)
        self._settlement: SettlementTracker = SettlementTracker(raw, db_path)

        # --- Strategy layer ---
        self._technical: TechnicalAnalyzer = TechnicalAnalyzer(config)
        self._entry_evaluator: EntryEvaluator = EntryEvaluator(
            config=config,
            technical=self._technical,
            sentiment=self._sentiment,
            earnings=self._earnings,
            market_data=self._market_data,
            fx=self._fx,
            settlement=self._settlement,
            db_path=db_path,
        )
        self._exit_manager: ExitManager = ExitManager(config, self._market_data, self._fx)

        # --- Execution layer ---
        self._order_manager: OrderManager = OrderManager(
            gateway=self._gateway,
            config=config,
            notifier=self._notifier,
            db_path=db_path,
        )
        self._risk_manager: RiskManager = RiskManager(
            config=config,
            db_path=db_path,
            fx=self._fx,
            notifier=self._notifier,
        )

        # --- Multi-strategy layer ---
        self._strategy_manager: StrategyManager | None = None
        if config.multi_strategy_enabled:
            strategy_configs: dict[str, Any] = config.get_strategy_configs()
            vol_target_cfg: dict[str, Any] = (
                config._raw.get("risk", {}) or {}
            ).get("vol_target", {}) or {}
            strategies = create_strategies(
                strategy_configs,
                db_path=db_path,
                vol_target_config=vol_target_cfg,
            )
            portfolio_mgr = PortfolioManager(
                strategy_configs=strategy_configs,
                total_cash=config.multi_strategy_total_allocation,
                db_path=db_path,
            )
            regime_cfg: dict[str, Any] = config._get("multi_strategy", "regime_filter", default={}) or {}
            regime_filter: RegimeFilter | None = None
            if bool(regime_cfg.get("enabled", True)):
                regime_filter = RegimeFilter(
                    get_daily_bars=self._get_daily_bars,
                    index_symbol=str(regime_cfg.get("index_symbol", "SPY")),
                    sma_period=int(regime_cfg.get("sma_period", 50)),
                    enabled=True,
                    cache_ttl_minutes=int(regime_cfg.get("cache_ttl_minutes", 30)),
                )
            loss_cd_cfg: dict[str, Any] = config.get_loss_cooldown_config()
            loss_cooldown: LossCooldownTracker = LossCooldownTracker(
                db_path=db_path,
                config=LossCooldownConfig(
                    enabled=bool(loss_cd_cfg.get("enabled", False)),
                    threshold_losses=int(loss_cd_cfg.get("threshold_losses", 3)),
                    cooldown_minutes=int(loss_cd_cfg.get("cooldown_minutes", 240)),
                ),
            )
            self._strategy_manager = StrategyManager(
                strategies=strategies,
                portfolio_manager=portfolio_mgr,
                market_data=self._market_data,
                order_manager=self._order_manager,
                risk_manager=self._risk_manager,
                sentiment=self._sentiment,
                earnings=self._earnings,
                config=config,
                db_path=db_path,
                dry_run=dry_run,
                regime_filter=regime_filter,
                loss_cooldown=loss_cooldown,
            )
            logger.info(
                "Multi-strategy enabled: %d strategies, $%.0f total",
                len(strategies), config.multi_strategy_total_allocation,
            )

        # --- Reporting ---
        self._performance: PerformanceCalculator = PerformanceCalculator(db_path)
        self._report_generator: ReportGenerator = ReportGenerator(config, self._performance)

        # --- State recovery ---
        self._state_recovery: StateRecovery = StateRecovery(
            gateway=self._gateway,
            db_path=db_path,
            notifier=self._notifier,
            config=raw,
        )

        # --- Per-tick runtime state ---
        # Active watchlist is rebuilt each tick from tick_state (pre-market
        # scan persists its ranked list), or falls back to config.watchlist.
        self._active_watchlist: dict[Market, list[str]] = {
            Market.US: [],
        }

        logger.info(
            "TradingBot initialised (phase=%s, mode=%s, account=%s, dry_run=%s)",
            config.get_phase().name,
            mode,
            config.account_id,
            dry_run,
        )
        if self._dry_run:
            logger.info("*** DRY RUN MODE — no orders will be placed ***")

    # ------------------------------------------------------------------
    # Tick entrypoint
    # ------------------------------------------------------------------

    async def tick(self) -> None:
        """Run one full scheduling cycle and exit.

        Responsibilities per tick:
        1. Connect to Alpaca (abort tick on failure).
        2. Refresh FX rate.
        3. Reconcile broker state with SQLite.
        4. Poll outstanding order statuses.
        5. For the US market, run pre-market scan / entry scan / exit check /
           wind-down depending on the current window.  Each stage is guarded by
           a day-scoped flag persisted to ``tick_state``.
        6. Run phase-transition + daily-summary checks once per day after
           wind-down has completed.
        """
        self._setup_logging()
        logger.info("=== tick start (mode=%s, dry_run=%s) ===", self._mode, self._dry_run)

        now_et: datetime = datetime.now(tz=_EASTERN)
        today_et: date = now_et.date()

        # --- 1. Trading-day gate ---
        if not self._config.is_trading_day(today_et, Market.US):
            logger.info("Non-trading day (%s) — tick exits", today_et.isoformat())
            return

        # --- 2. Operating-hours gate (bot_start_gmt..bot_end_gmt) ---
        now_utc: datetime = datetime.now(tz=TZ_UTC)
        bot_start_gmt: time = _parse_time(
            self._config._get("schedule", "bot_start_gmt") or "07:45"
        )
        bot_end_gmt: time = _parse_time(
            self._config._get("schedule", "bot_end_gmt") or "21:00"
        )
        if not (bot_start_gmt <= now_utc.time() <= bot_end_gmt):
            logger.info(
                "Outside operating hours (%s GMT) — tick exits",
                now_utc.strftime("%H:%M"),
            )
            return

        # --- 3. Connect to Alpaca ---
        connected: bool = await self._gateway.connect()
        if not connected:
            logger.error("Alpaca connect failed — tick aborts")
            return

        try:
            # --- 4. FX refresh ---
            try:
                await self._fx.refresh()
            except Exception:
                logger.warning("FX refresh failed — using cached rate", exc_info=True)

            # --- 5. State recovery (reconcile broker vs SQLite) ---
            try:
                recovery_result = await self._state_recovery.recover()
                logger.info("State recovery: %s", recovery_result.summary())
            except Exception:
                logger.exception("State recovery failed (non-fatal)")

            # --- 6. Poll order statuses ---
            try:
                await self._order_manager._check_order_statuses()
            except Exception:
                logger.warning("Order status poll failed", exc_info=True)

            # --- 7. Phase 0 one-time marker (Alpaca has no legacy cleanup) ---
            if not self._is_phase0_complete():
                self._record_phase_transition(
                    from_phase=0, to_phase=1,
                    reason="Alpaca account — no portfolio cleanup required",
                )

            # --- 8. Day-scoped flags (persisted via tick_state "__day__") ---
            flags: dict[str, Any] = self._load_day_flags(today_et)

            # --- 9. Daily risk counter reset (only once per day) ---
            if today_et != self._risk_manager._trading_day:
                self._risk_manager.reset_daily()

            # --- 10. Watchlist bootstrap: seed quotes via bulk REST ---
            watchlist_tickers: list[str] = self._config.get_watchlist(Market.US)
            try:
                await self._market_data.refresh_quotes(watchlist_tickers)
            except Exception:
                logger.warning("refresh_quotes failed for watchlist", exc_info=True)

            # --- 11. Pre-market scan (once per day, within any active window) ---
            market_active: bool = (
                self._is_market_in_premarket(Market.US)
                or self._is_market_in_execution(Market.US)
                or self._is_market_in_winddown(Market.US)
            )
            if (
                market_active
                and not flags.get("pre_market_done", False)
                and self._mode in ("premarket", "normal")
            ):
                try:
                    await self.pre_market_scan(Market.US)
                except Exception:
                    logger.exception("Pre-market scan failed")
                flags["pre_market_done"] = True
                # Persist the ranked watchlist too so the next tick can re-use it.
                flags["ranked_us_watchlist"] = self._active_watchlist.get(Market.US, [])
                self._save_day_flags(today_et, flags)
            else:
                # Restore ranked watchlist from flags if pre-market already ran
                ranked: list[str] = flags.get("ranked_us_watchlist") or []
                if ranked:
                    self._active_watchlist[Market.US] = ranked

            # --- 12. Entry scan ---
            if (
                self._is_market_in_execution(Market.US)
                and self._mode != "close-only"
            ):
                try:
                    await self.scan_for_entries(Market.US)
                except Exception:
                    logger.exception("Entry scan failed")

            # --- 13. Exit check (always) ---
            try:
                await self.check_exits()
            except Exception:
                logger.exception("Exit check failed")

            # --- 14. Wind-down (once per day within wind-down window) ---
            if (
                self._is_market_in_winddown(Market.US)
                and not flags.get("wind_down_done", False)
            ):
                try:
                    await self.wind_down(Market.US)
                except Exception:
                    logger.exception("Wind-down failed")
                flags["wind_down_done"] = True
                self._save_day_flags(today_et, flags)

            # --- 15. After-close daily tasks ---
            await self._maybe_check_phase_transition(today_et, flags)
            await self._maybe_save_daily_summary(today_et, flags)

        finally:
            # Always disconnect and close notifier session so file descriptors
            # aren't left dangling between cron invocations.
            try:
                await self._gateway.disconnect()
            except Exception:
                logger.warning("Gateway disconnect failed", exc_info=True)
            try:
                await self._notifier.shutdown()
            except Exception:
                logger.warning("Notifier shutdown failed", exc_info=True)

        logger.info("=== tick complete ===")

    # ------------------------------------------------------------------
    # Day-flag persistence (tick_state with strategy_id="__day__")
    # ------------------------------------------------------------------

    def _load_day_flags(self, today: date) -> dict[str, Any]:
        """Load day-scoped flags for *today*; reset if stored row is stale."""
        today_str: str = today.isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                row = repo.load_tick_state(conn, "__day__")
                if row and row.get("last_bar_ts") == today_str:
                    return dict(row.get("state") or {})
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to load day flags", exc_info=True)
        return {}

    def _save_day_flags(self, today: date, flags: dict[str, Any]) -> None:
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                repo.save_tick_state(
                    conn, "__day__", last_bar_ts=today.isoformat(), state=flags
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.warning("Failed to save day flags", exc_info=True)

    # ------------------------------------------------------------------
    # Pre-market scan
    # ------------------------------------------------------------------

    async def pre_market_scan(self, market: Market) -> None:
        """Pre-market scan:
        1. Refresh earnings calendar for all watchlist tickers.
        2. Refresh sentiment for all watchlist tickers.
        3. Subscribe to market data.
        4. Rank tickers: composite(sentiment, gap_pct) descending.
        5. Store result in self._active_watchlist[market].
        """
        logger.info("Pre-market scan starting for %s", market.value)
        watchlist: list[str] = self._config.get_watchlist(market)

        if not watchlist:
            logger.warning("Empty watchlist for %s — skipping pre-market scan", market.value)
            return

        exchange_str: str = "US"

        # 1. Refresh earnings calendar (single Finnhub call covers all tickers)
        try:
            await self._earnings.refresh(watchlist)
        except Exception:
            logger.warning("Earnings refresh failed", exc_info=True)

        # 2. Refresh sentiment (per-ticker, rate-limited inside refresh_all)
        try:
            await self._sentiment.refresh_all(watchlist)
        except Exception:
            # Benign: Finnhub often returns 403 on ETFs; sentiment is optional.
            logger.debug("Sentiment refresh failed", exc_info=True)

        # 3. Subscribe to market data (REST seed — no WebSocket wait needed)
        for ticker in watchlist:
            try:
                await self._market_data.subscribe(ticker, exchange_str)
            except Exception:
                logger.exception("Market data sub failed for %s", ticker)

        # 4. Rank tickers
        ranked: list[str] = await self._rank_watchlist(watchlist, market, exchange_str)

        # 5. Store
        self._active_watchlist[market] = ranked
        logger.info(
            "%s pre-market scan complete. Ranked watchlist (%d): %s",
            market.value,
            len(ranked),
            ranked,
        )

    async def _rank_watchlist(
        self, watchlist: list[str], market: Market, exchange_str: str
    ) -> list[str]:
        """Rank tickers by sentiment_score * (1 + gap_pct) descending."""
        scores: list[tuple[str, float]] = []

        for ticker in watchlist:
            try:
                # Sentiment (0.0 if unavailable)
                sentiment_score: float | None = await self._sentiment.get_sentiment(ticker)
                if sentiment_score is None:
                    sentiment_score = 0.0

                # Pre-market gap from overnight close
                gap_pct: float = 0.0
                current_price: float | None = self._market_data.get_latest_price(ticker)
                if current_price is not None and current_price > 0:
                    bars = await self._market_data.get_historical_bars(
                        ticker, exchange_str, bar_size="1 day", duration="2 D"
                    )
                    if bars and len(bars) >= 1:
                        prev_close: float = float(getattr(bars[-1], "close", 0))
                        if prev_close > 0:
                            gap_pct = abs(current_price - prev_close) / prev_close

                composite: float = (0.5 + sentiment_score) * (1.0 + gap_pct)
                scores.append((ticker, composite))
            except Exception:
                logger.warning("Ranking failed for %s", ticker, exc_info=True)
                scores.append((ticker, 0.0))

        scores.sort(key=lambda x: x[1], reverse=True)
        return [t for t, _ in scores]

    # ------------------------------------------------------------------
    # Scan for entries
    # ------------------------------------------------------------------

    async def scan_for_entries(self, market: Market) -> None:
        """Scan active watchlist for entry signals and place orders."""
        # Delegate to multi-strategy manager if enabled
        if self._strategy_manager is not None:
            watchlist: list[str] = (
                self._active_watchlist.get(market) or self._config.get_watchlist(market)
            )
            # NOTE: ``_get_account_equity_gbp`` returns USD on Alpaca
            # (NetLiquidation is reported in account currency = USD).
            # The function name is a legacy IBKR-era misnomer pending a
            # broader rename that also touches the daily_summaries DB
            # column. Pass through as USD here for the new
            # StrategyManager API.
            account_equity_usd: float = await self._get_account_equity_gbp()
            await self._strategy_manager.scan_for_entries(
                watchlist=watchlist,
                get_5min_bars=self._get_5min_bars,
                get_daily_bars=self._get_daily_bars,
                account_equity_usd=account_equity_usd,
            )
            return

        if self._market_data.trading_paused:
            logger.warning(
                "Market data paused (mass staleness) — skipping entry scan for %s",
                market.value,
            )
            return

        watchlist: list[str] = (
            self._active_watchlist.get(market) or self._config.get_watchlist(market)
        )
        if not watchlist:
            return

        account_equity_gbp: float = await self._get_account_equity_gbp()

        # Update risk manager with today's P&L
        self._risk_manager.check_daily_loss_limit(
            self._risk_manager.daily_pnl_gbp, account_equity_gbp
        )

        exchange_str: str = "US"

        for ticker in watchlist:
            # Top-level risk gate before each candidate
            can_trade, reason = self._risk_manager.can_trade()
            if not can_trade:
                logger.info(
                    "Risk manager blocking new entries (%s) — stopping scan for %s",
                    reason,
                    market.value,
                )
                break

            # Skip stale data
            if self._market_data.is_stale(ticker):
                logger.debug("%s: stale data — skipping", ticker)
                continue

            # Skip earnings blackout
            if self._earnings.is_in_blackout(ticker, datetime.now(tz=_EASTERN)):
                logger.debug("%s: earnings blackout — skipping", ticker)
                continue

            # Skip cooldown
            if self._is_on_cooldown(ticker):
                logger.debug("%s: on cooldown — skipping", ticker)
                continue

            # Fetch bars
            try:
                df_5min = await self._get_5min_bars(ticker, exchange_str)
                df_daily = await self._get_daily_bars(ticker, exchange_str)
            except Exception:
                logger.warning("Bar fetch failed for %s", ticker, exc_info=True)
                continue

            if df_5min is None or df_5min.empty or df_daily is None or df_daily.empty:
                logger.debug("%s: insufficient bars — skipping", ticker)
                continue

            # Evaluate entry signal
            try:
                decision: EntryDecision = await self._entry_evaluator.evaluate(
                    ticker=ticker,
                    exchange=exchange_str,
                    df_5min=df_5min,
                    df_daily=df_daily,
                    account_equity_gbp=account_equity_gbp,
                )
            except Exception:
                logger.warning("Entry evaluation error for %s", ticker, exc_info=True)
                continue

            if not decision.should_enter:
                continue

            # Final risk gate (race-condition guard)
            can_trade, reason = self._risk_manager.can_trade()
            if not can_trade:
                logger.info("Risk gate blocked entry for %s: %s", ticker, reason)
                break

            # Build and place the order
            om_decision: OMEntryDecision | None = self._build_om_decision(decision)
            if om_decision is None:
                continue

            logger.info(
                "Entry signal: %s %s %d shares @ %.4f",
                ticker,
                decision.direction,
                decision.position_size or 0,
                decision.signal_price or 0.0,
            )

            if self._dry_run:
                logger.info(
                    "[DRY RUN] Would enter: %s %s %d shares @ %.4f, stop=%.4f, target=%.4f",
                    ticker,
                    decision.direction,
                    decision.position_size or 0,
                    decision.signal_price or 0.0,
                    decision.stop_price or 0.0,
                    decision.target_price or 0.0,
                )
                continue

            trade_id: int | None = await self._order_manager.place_entry(om_decision)
            if trade_id is not None:
                logger.info("Entry order placed: %s trade_id=%d", ticker, trade_id)
            else:
                logger.warning("Entry order failed: %s", ticker)

    def _build_om_decision(self, decision: EntryDecision) -> OMEntryDecision | None:
        """Convert strategy EntryDecision to order-manager EntryDecision."""
        if (
            decision.position_size is None
            or decision.signal_price is None
            or decision.stop_price is None
            or decision.target_price is None
            or decision.hold_type is None
            or decision.direction is None
        ):
            logger.warning(
                "%s: incomplete EntryDecision — cannot build OM order",
                decision.ticker,
            )
            return None

        sector: str = GICS_SECTOR.get(decision.ticker, "Unknown")
        currency: str = "USD"

        return OMEntryDecision(
            ticker=decision.ticker,
            exchange=decision.exchange,
            side="BUY" if decision.direction == "long" else "SELL",
            shares=decision.position_size,
            limit_price=decision.signal_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price,
            hold_type=decision.hold_type.value,
            sector=sector,
            phase=self._config.get_phase().value,
            sentiment_score=decision.sentiment_score,
            signals=json.dumps(decision.signals) if decision.signals else "",
            currency=currency,
        )

    def _is_on_cooldown(self, ticker: str) -> bool:
        """Return True if ticker is in a post-exit cooldown window."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cooldown_until: datetime | None = repo.get_cooldown(conn, ticker)
            conn.close()
        except Exception:
            logger.warning("Cooldown lookup failed for %s", ticker, exc_info=True)
            return False

        if cooldown_until is None:
            return False

        now_et: datetime = datetime.now(tz=_EASTERN)
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=_EASTERN)
        return now_et < cooldown_until

    # ------------------------------------------------------------------
    # Check exits
    # ------------------------------------------------------------------

    async def check_exits(self) -> None:
        """Check all open positions for exit conditions.

        Runs on every main-loop iteration regardless of market window.
        """
        # Activate deferred trailing stops (once price crosses activation)
        try:
            await self._order_manager.check_trail_activations(
                get_latest_price=self._market_data.get_latest_price,
            )
        except Exception:
            logger.warning("Trail-activation check failed", exc_info=True)

        # Delegate strategy-tagged positions to multi-strategy manager
        if self._strategy_manager is not None:
            await self._strategy_manager.check_exits(
                get_5min_bars=self._get_5min_bars,
                get_daily_bars=self._get_daily_bars,
            )
            # Fall through to also check legacy (untagged) positions below

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            open_positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
        except Exception:
            logger.exception("Failed to load open positions for exit check")
            return

        if not open_positions:
            return

        now_et: datetime = datetime.now(tz=_EASTERN)

        for position in open_positions:
            ticker: str = position.get("ticker", "")
            exchange: str = position.get("exchange", "SMART")
            status: str = position.get("status", "")

            # Skip positions that are already being closed
            if status in (PositionStatus.CLOSING.value, PositionStatus.CLOSED.value):
                continue

            # Require a current price
            current_price: float | None = self._market_data.get_latest_price(ticker)
            if current_price is None:
                logger.debug("%s: no price for exit check", ticker)
                continue

            # Evaluate exit conditions
            try:
                exit_decision = self._exit_manager.should_exit(
                    position=position,
                    current_price=current_price,
                    current_time=now_et,
                )
            except Exception:
                logger.warning("Exit evaluation error for %s", ticker, exc_info=True)
                continue

            if not exit_decision.should_exit:
                continue

            logger.info(
                "Exit triggered: %s reason=%s emergency=%s price=%.4f",
                ticker,
                exit_decision.reason,
                exit_decision.is_emergency,
                current_price,
            )

            # Spread check for non-emergency exits. Track first-defer time
            # in tick_state; once the spread has been wide for longer than
            # ``spread_max_delay_seconds``, force a market exit instead of
            # deferring again.
            force_market_exit: bool = False
            if not exit_decision.is_emergency:
                if not self._exit_manager.check_spread_for_exit(ticker):
                    defer_resolved = self._resolve_spread_defer(
                        ticker=ticker,
                        now=now_et,
                        max_delay_s=self._exit_manager.spread_max_delay_seconds,
                    )
                    if not defer_resolved:
                        logger.info(
                            "%s: spread too wide for non-emergency exit — deferring",
                            ticker,
                        )
                        continue
                    logger.warning(
                        "%s: spread-defer age exceeded %ds — forcing market exit",
                        ticker,
                        self._exit_manager.spread_max_delay_seconds,
                    )
                    force_market_exit = True
                else:
                    self._clear_spread_defer(ticker)

            qty: int = int(position.get("quantity", 0))
            if qty <= 0:
                logger.warning("%s: exit triggered but qty=0 — skipping", ticker)
                continue

            if self._dry_run:
                logger.info(
                    "[DRY RUN] Would exit: SELL %d %s @ %.4f (reason=%s)",
                    qty, ticker, current_price, exit_decision.reason,
                )
                continue

            # Cancel existing orders for this ticker first
            await self._order_manager.cancel_all_for_ticker(ticker)

            if exit_decision.use_market_order or force_market_exit:
                await self._order_manager.emergency_flatten(ticker, qty, exchange)
                self._clear_spread_defer(ticker)
            else:
                # Limit sell at mid-price
                bid_ask = self._market_data.get_bid_ask(ticker)
                limit_price: float
                if bid_ask is not None:
                    limit_price = (bid_ask[0] + bid_ask[1]) / 2.0
                else:
                    limit_price = current_price

                try:
                    from alpaca.trading.requests import LimitOrderRequest
                    from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

                    request = LimitOrderRequest(
                        symbol=ticker,
                        qty=qty,
                        side=OrderSide.SELL,
                        type=OrderType.LIMIT,
                        time_in_force=TimeInForce.DAY,
                        limit_price=round(limit_price, 2),
                    )
                    self._gateway.client.submit_order(order_data=request)
                    logger.info(
                        "Exit limit order: SELL %d %s @ %.2f (reason=%s)",
                        qty, ticker, limit_price, exit_decision.reason,
                    )
                except Exception:
                    logger.exception(
                        "Limit exit failed for %s — falling back to market", ticker
                    )
                    await self._order_manager.emergency_flatten(ticker, qty, exchange)
                else:
                    self._clear_spread_defer(ticker)

    # ------------------------------------------------------------------
    # Spread defer tracking (per-ticker tick_state rows)
    # ------------------------------------------------------------------

    def _spread_defer_key(self, ticker: str) -> str:
        return f"spread_defer:{ticker}"

    def _resolve_spread_defer(
        self, *, ticker: str, now: datetime, max_delay_s: int,
    ) -> bool:
        """Record/inspect spread-defer state for *ticker*.

        Returns True iff the ticker has been deferred for longer than
        ``max_delay_s`` and the caller should force a market exit; False
        means the caller should defer this tick.
        """
        key: str = self._spread_defer_key(ticker)
        now_iso: str = now.isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                row = repo.load_tick_state(conn, key)
                if row is None:
                    repo.save_tick_state(conn, key, last_bar_ts=now_iso, state={})
                    return False
                first_iso: str | None = row.get("last_bar_ts")
                if not first_iso:
                    repo.save_tick_state(conn, key, last_bar_ts=now_iso, state={})
                    return False
                try:
                    first_dt: datetime = datetime.fromisoformat(first_iso)
                except ValueError:
                    repo.save_tick_state(conn, key, last_bar_ts=now_iso, state={})
                    return False
                if first_dt.tzinfo is None:
                    first_dt = first_dt.replace(tzinfo=_EASTERN)
                return (now - first_dt).total_seconds() >= max_delay_s
            finally:
                conn.close()
        except Exception:
            logger.exception("spread-defer bookkeeping failed for %s", ticker)
            return False

    def _clear_spread_defer(self, ticker: str) -> None:
        """Remove a spread-defer record after a successful exit."""
        key: str = self._spread_defer_key(ticker)
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    "DELETE FROM tick_state WHERE strategy_id = ?", (key,),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            logger.debug("clear spread-defer failed for %s", ticker, exc_info=True)

    # ------------------------------------------------------------------
    # Wind-down
    # ------------------------------------------------------------------

    async def wind_down(self, market: Market) -> None:
        """Close all intraday positions for a market. Swing positions exempt.

        Places a limit sell at mid-price; if unfilled after 5 minutes,
        switches to a market order.
        """
        logger.info("Wind-down started for %s", market.value)

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            open_positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
        except Exception:
            logger.exception("Failed to load positions for wind-down")
            return

        for position in open_positions:
            ticker: str = position.get("ticker", "")
            hold_type: str = position.get("hold_type", "intraday")
            status: str = position.get("status", "")
            exchange: str = position.get("exchange", "SMART")

            # Swing positions are exempt
            if hold_type == HoldType.SWING.value:
                logger.info("%s: swing — exempt from wind-down", ticker)
                continue

            if status in (PositionStatus.CLOSING.value, PositionStatus.CLOSED.value):
                continue

            # Only close positions belonging to this market
            ticker_market: Market | None = self._get_ticker_market(ticker)
            if ticker_market is not None and ticker_market != market:
                continue

            qty: int = int(position.get("quantity", 0))
            if qty <= 0:
                continue

            logger.info(
                "Wind-down: closing intraday %s (%d shares) for %s market",
                ticker, qty, market.value,
            )

            if self._dry_run:
                current_price: float | None = self._market_data.get_latest_price(ticker)
                logger.info(
                    "[DRY RUN] Would wind-down: SELL %d %s @ %.4f",
                    qty, ticker, current_price or 0.0,
                )
                continue

            # Cancel existing orders
            await self._order_manager.cancel_all_for_ticker(ticker)

            # Determine limit price
            bid_ask = self._market_data.get_bid_ask(ticker)
            current_price: float | None = self._market_data.get_latest_price(ticker)

            if bid_ask is not None:
                limit_price: float = (bid_ask[0] + bid_ask[1]) / 2.0
            elif current_price is not None:
                limit_price = current_price
            else:
                logger.warning(
                    "%s: no price for wind-down — using emergency market flatten",
                    ticker,
                )
                await self._order_manager.emergency_flatten(ticker, qty, exchange)
                continue

            # Tick-model wind-down: place a DAY limit sell and exit.  Alpaca
            # auto-cancels the DAY order at close, so any unfilled portion
            # becomes a market-on-close candidate for the next wind-down tick
            # (or for the exit-check logic driving emergency_flatten).
            try:
                from alpaca.trading.requests import LimitOrderRequest
                from alpaca.trading.enums import OrderSide, OrderType, TimeInForce

                request = LimitOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.SELL,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round(limit_price, 2),
                )
                order = self._gateway.client.submit_order(order_data=request)
                logger.info(
                    "Wind-down limit placed: SELL %d %s @ %.2f (order_id=%s)",
                    qty, ticker, limit_price, str(order.id),
                )
            except Exception:
                logger.exception(
                    "Wind-down order error for %s — trying market flatten", ticker
                )
                await self._order_manager.emergency_flatten(ticker, qty, exchange)

        logger.info("Wind-down complete for %s", market.value)

    # ------------------------------------------------------------------
    # Phase 0 cleanup orchestration was removed: the Alpaca account was
    # opened fresh with no inherited positions, ``tick()`` short-circuits
    # the (0 -> 1) transition by recording it directly in
    # ``phase_transitions``, and the legacy ``run_phase0`` method was
    # never called from anywhere in the live codebase.  See
    # ``trading_bot/strategy/portfolio_assessor.py`` if you need to
    # resurrect the scoring/cleanup logic.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Phase transition check
    # ------------------------------------------------------------------

    async def _check_phase_transition(self, account_equity_gbp: float) -> None:
        """Daily phase transition check using equity and win-rate metrics."""
        current_phase: Phase = self._config.get_phase()
        raw_cfg: dict[str, Any] = self._config._raw

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            summaries: list[dict[str, Any]] = repo.get_recent_daily_summaries(
                conn, n_days=60
            )
            conn.close()
        except Exception:
            logger.exception("Failed to load daily summaries for phase check")
            return

        trading_days: int = len(summaries)

        # --- Promotion: Phase 1 (MICRO) -> Phase 2 (SMALL) ---
        if current_phase == Phase.MICRO:
            p2_cfg: dict[str, Any] = (
                raw_cfg.get("phases", {}).get("phase1_to_phase2", {})
            )
            eq_threshold: float = float(p2_cfg.get("equity_gbp", 5000))
            min_days: int = int(p2_cfg.get("min_trading_days", 40))
            min_wr: float = float(p2_cfg.get("min_win_rate_last_n", 0.52))
            wr_lookback: int = int(p2_cfg.get("win_rate_lookback_trades", 20))

            if account_equity_gbp >= eq_threshold and trading_days >= min_days:
                recent_wr: float = self._calc_recent_win_rate(wr_lookback)
                if recent_wr >= min_wr:
                    logger.info(
                        "Phase promotion: MICRO -> SMALL (equity=£%.2f, wr=%.1f%%)",
                        account_equity_gbp, recent_wr * 100,
                    )
                    self._record_phase_transition(
                        from_phase=Phase.MICRO.value,
                        to_phase=Phase.SMALL.value,
                        reason=f"equity={account_equity_gbp:.2f}, win_rate={recent_wr:.3f}",
                        equity=account_equity_gbp,
                    )
                    await self._notifier.phase_transition(
                        from_phase=Phase.MICRO.value,
                        to_phase=Phase.SMALL.value,
                        equity=account_equity_gbp,
                    )
                    self._config._phase = None  # invalidate cache

        # --- Promotion: Phase 2 (SMALL) -> Phase 3 (FULL) ---
        elif current_phase == Phase.SMALL:
            p3_cfg: dict[str, Any] = (
                raw_cfg.get("phases", {}).get("phase2_to_phase3", {})
            )
            eq_threshold = float(p3_cfg.get("equity_gbp", 20000))
            min_days = int(p3_cfg.get("min_trading_days", 60))
            min_wr = float(p3_cfg.get("min_win_rate_last_n", 0.55))
            wr_lookback = int(p3_cfg.get("win_rate_lookback_trades", 40))

            if account_equity_gbp >= eq_threshold and trading_days >= min_days:
                recent_wr = self._calc_recent_win_rate(wr_lookback)
                if recent_wr >= min_wr:
                    logger.info(
                        "Phase promotion: SMALL -> FULL (equity=£%.2f, wr=%.1f%%)",
                        account_equity_gbp, recent_wr * 100,
                    )
                    self._record_phase_transition(
                        from_phase=Phase.SMALL.value,
                        to_phase=Phase.FULL.value,
                        reason=f"equity={account_equity_gbp:.2f}, win_rate={recent_wr:.3f}",
                        equity=account_equity_gbp,
                    )
                    await self._notifier.phase_transition(
                        from_phase=Phase.SMALL.value,
                        to_phase=Phase.FULL.value,
                        equity=account_equity_gbp,
                    )
                    self._config._phase = None

        # --- Demotion checks ---
        demotion_pct: float = float(
            raw_cfg.get("phases", {}).get("demotion", {}).get(
                "equity_pct_of_threshold", 0.80
            )
        )

        if current_phase == Phase.SMALL:
            p2_threshold: float = float(
                raw_cfg.get("phases", {}).get("phase1_to_phase2", {}).get("equity_gbp", 5000)
            )
            demotion_threshold: float = p2_threshold * demotion_pct
            if account_equity_gbp < demotion_threshold:
                logger.warning(
                    "Phase demotion: SMALL -> MICRO (equity=£%.2f < £%.2f)",
                    account_equity_gbp, demotion_threshold,
                )
                self._record_phase_transition(
                    from_phase=Phase.SMALL.value,
                    to_phase=Phase.MICRO.value,
                    reason=f"demotion: equity={account_equity_gbp:.2f}",
                    equity=account_equity_gbp,
                )
                await self._notifier.phase_transition(
                    from_phase=Phase.SMALL.value,
                    to_phase=Phase.MICRO.value,
                    equity=account_equity_gbp,
                )
                self._config._phase = None

        elif current_phase == Phase.FULL:
            p3_threshold: float = float(
                raw_cfg.get("phases", {}).get("phase2_to_phase3", {}).get("equity_gbp", 20000)
            )
            demotion_threshold = p3_threshold * demotion_pct
            if account_equity_gbp < demotion_threshold:
                logger.warning(
                    "Phase demotion: FULL -> SMALL (equity=£%.2f < £%.2f)",
                    account_equity_gbp, demotion_threshold,
                )
                self._record_phase_transition(
                    from_phase=Phase.FULL.value,
                    to_phase=Phase.SMALL.value,
                    reason=f"demotion: equity={account_equity_gbp:.2f}",
                    equity=account_equity_gbp,
                )
                await self._notifier.phase_transition(
                    from_phase=Phase.FULL.value,
                    to_phase=Phase.SMALL.value,
                    equity=account_equity_gbp,
                )
                self._config._phase = None

    def _calc_recent_win_rate(self, lookback_trades: int) -> float:
        """Calculate win rate from the N most recent closed trades."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT pnl_gbp FROM trades
                WHERE exit_time IS NOT NULL
                ORDER BY exit_time DESC
                LIMIT ?
                """,
                (lookback_trades,),
            ).fetchall()
            conn.close()
            if not rows:
                return 0.0
            wins: int = sum(1 for r in rows if (r["pnl_gbp"] or 0) > 0)
            return wins / len(rows)
        except Exception:
            logger.warning("Win rate calculation failed", exc_info=True)
            return 0.0

    def _record_phase_transition(
        self,
        from_phase: int,
        to_phase: int,
        reason: str,
        equity: float = 0.0,
    ) -> None:
        """Persist a phase_transitions record."""
        direction: str = "promotion" if to_phase > from_phase else "demotion"
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            repo.save_phase_transition(
                conn,
                {
                    "date": datetime.now(tz=_EASTERN).strftime("%Y-%m-%d"),
                    "from_phase": from_phase,
                    "to_phase": to_phase,
                    "direction": direction,
                    "account_equity_gbp": equity,
                    "metrics_json": json.dumps({"reason": reason}),
                    "reason": reason,
                },
            )
            conn.close()
            logger.info(
                "Phase transition recorded: %d -> %d (%s) reason=%s",
                from_phase, to_phase, direction, reason,
            )
        except Exception:
            logger.exception("Failed to record phase transition %d->%d", from_phase, to_phase)

    # ------------------------------------------------------------------
    # Market window helpers
    # ------------------------------------------------------------------

    def _is_market_in_execution(self, market: Market) -> bool:
        """Return True if market is currently in its execution window."""
        tz: ZoneInfo = _MARKET_TZ[market]
        now_local: time = datetime.now(tz=tz).time()
        exec_start: time = self._config.get_execution_start(market)
        exec_end: time = self._config.get_execution_end(market)
        return exec_start <= now_local < exec_end

    def _is_market_in_premarket(self, market: Market) -> bool:
        """Return True if market is currently in its pre-market scan window."""
        tz: ZoneInfo = _MARKET_TZ[market]
        now_local: time = datetime.now(tz=tz).time()
        schedule: dict[str, Any] = self._config._schedule_section(market)
        scan_start: time = _parse_time(
            schedule.get("pre_market_scan_start") or "07:45"
        )
        scan_end: time = _parse_time(
            schedule.get("pre_market_scan_end") or "08:00"
        )
        return scan_start <= now_local < scan_end

    def _is_market_in_winddown(self, market: Market) -> bool:
        """Return True if market is currently in its wind-down window."""
        tz: ZoneInfo = _MARKET_TZ[market]
        now_local: time = datetime.now(tz=tz).time()
        wd_start: time = self._config.get_wind_down_start(market)
        wd_end: time = self._config.get_wind_down_end(market)
        return wd_start <= now_local <= wd_end

    # ------------------------------------------------------------------
    # Logging setup
    # ------------------------------------------------------------------

    def _setup_logging(self) -> None:
        """Configure daily-rotating file handler + console INFO+.

        Log files are named ``bot.log`` (current day) with rotated files
        suffixed by date, e.g. ``bot.log.2026-04-17``.  By default, 30 days
        of history are kept.
        """
        log_level_str: str = self._config.log_level.upper()
        log_level: int = getattr(logging, log_level_str, logging.INFO)
        log_format: str = self._config.log_format
        log_file: str = self._config.log_file
        backup_count: int = self._config.log_backup_count

        Path(log_file).parent.mkdir(parents=True, exist_ok=True)

        root: logging.Logger = logging.getLogger()
        root.setLevel(log_level)
        formatter: logging.Formatter = logging.Formatter(log_format)

        # Daily-rotating file handler — rotates at midnight, keeps backup_count days
        file_handler: logging.handlers.TimedRotatingFileHandler = (
            logging.handlers.TimedRotatingFileHandler(
                log_file,
                when="midnight",
                interval=1,
                backupCount=backup_count,
                encoding="utf-8",
                utc=False,
            )
        )
        file_handler.setLevel(log_level)
        file_handler.setFormatter(formatter)

        # Console handler (INFO and above)
        console_handler: logging.StreamHandler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)

        # Avoid duplicating handlers if already configured
        has_file: bool = any(
            isinstance(h, (logging.handlers.RotatingFileHandler,
                           logging.handlers.TimedRotatingFileHandler))
            for h in root.handlers
        )
        has_console: bool = any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, (logging.handlers.RotatingFileHandler,
                                   logging.handlers.TimedRotatingFileHandler))
            for h in root.handlers
        )

        if not has_file:
            root.addHandler(file_handler)
        if not has_console:
            root.addHandler(console_handler)

    # ------------------------------------------------------------------
    # Daily task helpers (driven by tick-scoped flags)
    # ------------------------------------------------------------------

    async def _maybe_check_phase_transition(
        self, today: date, flags: dict[str, Any]
    ) -> None:
        """Run phase transition check once per day, after US market close."""
        if flags.get("phase_check_done", False):
            return

        now_et: datetime = datetime.now(tz=_EASTERN)
        us_wd_end: time = self._config.get_wind_down_end(Market.US)
        check_after_minute: int = (us_wd_end.minute + 5) % 60
        check_after_hour: int = us_wd_end.hour + (us_wd_end.minute + 5) // 60
        check_after: time = time(check_after_hour % 24, check_after_minute)

        if now_et.time() < check_after:
            return

        equity: float = await self._get_account_equity_gbp()
        if equity > 0:
            await self._check_phase_transition(equity)
        flags["phase_check_done"] = True
        self._save_day_flags(today, flags)

    async def _maybe_save_daily_summary(
        self, today: date, flags: dict[str, Any]
    ) -> None:
        """Save daily summary once per day, shortly after US wind-down ends."""
        if flags.get("daily_summary_saved", False):
            return

        now_et: datetime = datetime.now(tz=_EASTERN)
        us_wd_end: time = self._config.get_wind_down_end(Market.US)
        save_minute: int = (us_wd_end.minute + 10) % 60
        save_hour: int = us_wd_end.hour + (us_wd_end.minute + 10) // 60
        save_after: time = time(save_hour % 24, save_minute)

        if now_et.time() < save_after:
            return

        await self._save_daily_summary(today)
        flags["daily_summary_saved"] = True
        self._save_day_flags(today, flags)

    async def _save_daily_summary(self, today: date) -> None:
        """Compute and persist daily metrics, then send a summary notification."""
        today_str: str = today.isoformat()
        equity: float = await self._get_account_equity_gbp()

        try:
            metrics: dict[str, Any] = self._performance.calculate_daily_metrics(today_str)
        except Exception:
            logger.exception("Daily metrics calculation failed for %s", today_str)
            metrics = {}

        summary: dict[str, Any] = {
            "date": today_str,
            "total_trades": metrics.get("total_trades", 0),
            "wins": metrics.get("wins", 0),
            "losses": metrics.get("losses", 0),
            "gross_pnl_gbp": metrics.get("gross_pnl_gbp", 0.0),
            "commissions_gbp": metrics.get("commissions_gbp", 0.0),
            "net_pnl_gbp": metrics.get("net_pnl_gbp", 0.0),
            "account_equity_gbp": equity,
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "win_rate": metrics.get("win_rate"),
            "avg_win_gbp": metrics.get("avg_win"),
            "avg_loss_gbp": metrics.get("avg_loss"),
            "profit_factor": metrics.get("profit_factor"),
            "phase": self._config.get_phase().value,
            "lse_trades": metrics.get("lse_trades", 0),
            "us_trades": metrics.get("us_trades", 0),
            "commission_ratio": metrics.get("commission_ratio"),
            "notes": None,
        }

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            repo.save_daily_summary(conn, summary)
            conn.close()
            logger.info("Daily summary saved for %s (trades=%d, net_pnl=£%.2f)",
                        today_str, summary["total_trades"], summary["net_pnl_gbp"])
        except Exception:
            logger.exception("Failed to save daily summary for %s", today_str)

        try:
            await self._notifier.daily_summary(
                trades=summary["total_trades"],
                net_pnl=summary["net_pnl_gbp"],
                win_rate=summary.get("win_rate") or 0.0,
            )
        except Exception:
            logger.warning("Daily summary notification failed", exc_info=True)

        # Auto-generate daily HTML report
        try:
            path: str = self._report_generator.generate_daily_report(today_str)
            logger.info("Daily report auto-generated: %s", path)
        except Exception:
            logger.exception("Failed to auto-generate daily report for %s", today_str)

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    async def _get_account_equity_gbp(self) -> float:
        """Return account NetLiquidation in GBP; 0.0 on failure."""
        try:
            summary: dict[str, Any] = await self._gateway.get_account_summary()
            equity_str: str | None = summary.get("NetLiquidation")
            if equity_str:
                return float(equity_str)
        except Exception:
            logger.warning("get_account_equity_gbp failed", exc_info=True)
        return 0.0

    def _count_open_positions(self) -> int:
        """Count non-closed positions in SQLite."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
            return len(positions)
        except Exception:
            logger.warning("Failed to count open positions", exc_info=True)
            return 0

    def _is_phase0_complete(self) -> bool:
        """Return True if a Phase 0 -> Phase 1 transition has been recorded."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            result: bool = repo.is_phase0_complete(conn)
            conn.close()
            return result
        except Exception:
            logger.warning("Phase 0 check failed", exc_info=True)
            return False

    def _get_ticker_market(self, ticker: str) -> Market | None:
        """Return the logical Market for a ticker, or None if unknown."""
        from trading_bot.constants import ticker_market as _tm
        try:
            return _tm(ticker)
        except KeyError:
            return None

    async def _get_5min_bars(
        self, ticker: str, exchange: str,
    ) -> Any | None:
        """Fetch 5-minute bars and return as a pandas DataFrame, or None.

        ``MarketDataManager.get_historical_bars`` returns ``list[dict]`` with
        keys (open, high, low, close, volume, date).
        """
        import pandas as pd

        bars = await self._market_data.get_historical_bars(
            ticker, exchange, bar_size="5 mins", duration="2 D"
        )
        if not bars:
            return None

        df = pd.DataFrame(bars)
        if "date" in df.columns:
            df = df.set_index("date")
        return df

    async def _get_daily_bars(
        self, ticker: str, exchange: str,
    ) -> Any | None:
        """Fetch daily bars and return as a pandas DataFrame, or None."""
        import pandas as pd

        bars = await self._market_data.get_historical_bars(
            ticker, exchange, bar_size="1 day", duration="120 D"
        )
        if not bars:
            return None

        df = pd.DataFrame(bars)
        if "date" in df.columns:
            df = df.set_index("date")
        return df

# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _parse_time(value: str) -> time:
    """Parse an ``'HH:MM'`` string into a ``datetime.time`` object."""
    parts: list[str] = str(value).strip().split(":")
    return time(hour=int(parts[0]), minute=int(parts[1]))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="trading_bot",
        description="Alpaca Trading Bot orchestrator",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    parser.add_argument(
        "--mode",
        choices=["premarket", "normal", "close-only"],
        default="normal",
        help=(
            "premarket: start in pre-market scan mode; "
            "normal: skip pre-market, go straight to execution; "
            "close-only: no new entries, manage existing positions only"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Run the full bot flow but never place real orders; log what would happen instead",
    )
    return parser.parse_args()


async def _async_main() -> None:
    """Async entrypoint: parse args, load config, run a single tick."""
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config: Config = Config.load(args.config)
    bot: TradingBot = TradingBot(config, mode=args.mode, dry_run=args.dry_run)

    try:
        await bot.tick()
    except Exception:
        logger.exception("TradingBot tick raised an unhandled exception")
        raise


def main() -> None:
    """Synchronous entry point (``python -m trading_bot.main``)."""
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()

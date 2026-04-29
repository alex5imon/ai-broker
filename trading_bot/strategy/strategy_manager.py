"""Strategy manager — orchestrates multiple strategies in the main loop."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.constants import GICS_SECTOR, PositionStatus, TZ_EASTERN
from trading_bot.data.event_calendar import fomc_size_multiplier
from trading_bot.db import repository as repo
from trading_bot.execution.loss_cooldown import LossCooldownTracker
from trading_bot.execution.order_manager import EntryDecision as OMEntryDecision
from trading_bot.execution.virtual_portfolio import PortfolioManager, VirtualPortfolio
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.regime_filter import RegimeFilter

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


class StrategyManager:
    """Orchestrates multiple strategies against the watchlist."""

    def __init__(
        self,
        strategies: list[StrategyBase],
        portfolio_manager: PortfolioManager,
        market_data: Any,
        order_manager: Any,
        risk_manager: Any,
        sentiment: Any,
        earnings: Any,
        config: Any,
        db_path: str,
        dry_run: bool = False,
        regime_filter: RegimeFilter | None = None,
        loss_cooldown: LossCooldownTracker | None = None,
    ) -> None:
        self._strategies: list[StrategyBase] = strategies
        self._portfolio_manager: PortfolioManager = portfolio_manager
        self._market_data = market_data
        self._order_manager = order_manager
        self._risk_manager = risk_manager
        self._sentiment = sentiment
        self._earnings = earnings
        self._config = config
        self._db_path: str = db_path
        self._dry_run: bool = dry_run
        self._regime_filter: RegimeFilter | None = regime_filter
        self._loss_cooldown: LossCooldownTracker | None = loss_cooldown

    @property
    def strategies(self) -> list[StrategyBase]:
        return list(self._strategies)

    async def scan_for_entries(
        self,
        watchlist: list[str],
        get_5min_bars: Any,
        get_daily_bars: Any,
        account_equity_usd: float,
    ) -> int:
        """Run all strategies against the watchlist. Returns number of entries placed.

        ``account_equity_usd`` is the broker NetLiquidation in USD (Alpaca
        accounts settle in USD). The legacy "GBP" naming used elsewhere
        in main.py is a relic — this manager uses the correct currency
        label so future FX work won't accidentally apply a conversion to
        an already-USD figure.
        """
        entries_placed: int = 0
        # Per-strategy attempt counter for THIS scan. Counts every entry
        # we tried to place — successful, rejected, or recorded-only — so
        # ``max_positions`` is enforced even when the broker rejects us
        # or the in-memory portfolio hasn't refreshed yet. This was the
        # 2026-04-29 within-tick over-firing path.
        attempts_by_strategy: dict[str, int] = {}

        if self._market_data.trading_paused:
            logger.warning("Market data paused — skipping multi-strategy entry scan")
            return 0

        if self._regime_filter is not None:
            try:
                if not await self._regime_filter.allows_new_entries():
                    logger.info(
                        "Regime filter blocking new entries — bearish market regime",
                    )
                    return 0
            except Exception:
                logger.warning("Regime filter check failed — proceeding", exc_info=True)

        # Macro-event gate (FOMC days). Returns 0.0 to skip entirely, or a
        # multiplier in (0, 1] to scale risk down. The multiplier path is
        # plumbed below into per-decision share counts.
        event_mult: float = self._fomc_size_multiplier()
        if event_mult <= 0.0:
            logger.info(
                "Event gate (FOMC) blocking new entries for today — entries skipped",
            )
            return 0
        if event_mult < 1.0:
            logger.info(
                "Event gate (FOMC) reducing entry size by %.0f%% today",
                (1.0 - event_mult) * 100,
            )

        for ticker in watchlist:
            can_trade, reason = self._risk_manager.can_trade()
            if not can_trade:
                logger.info("Risk manager blocking entries: %s", reason)
                break

            if self._market_data.is_stale(ticker):
                continue
            if self._earnings.is_in_blackout(ticker, datetime.now(tz=ET)):
                continue

            try:
                df_5min: pd.DataFrame | None = await get_5min_bars(ticker, "US")
                df_daily: pd.DataFrame | None = await get_daily_bars(ticker, "US")
            except Exception:
                logger.warning("Bar fetch failed for %s", ticker, exc_info=True)
                continue

            if df_5min is None or df_5min.empty or df_daily is None or df_daily.empty:
                continue

            current_price: float | None = self._market_data.get_latest_price(ticker)
            if current_price is None or current_price <= 0:
                # Fall back to the most recent 5-min bar close when the live
                # stream hasn't delivered a tick (common on free IEX paper).
                try:
                    current_price = float(df_5min.iloc[-1]["close"])
                except Exception:
                    current_price = None
            if current_price is None or current_price <= 0:
                continue

            sentiment_score: float | None = None
            try:
                sentiment_score = await self._sentiment.get_sentiment(ticker)
            except Exception:
                # Benign: sentiment is optional (Finnhub 403s on ETFs etc.).
                logger.debug("Sentiment lookup failed for %s", ticker, exc_info=True)

            for strategy in self._strategies:
                portfolio: VirtualPortfolio | None = self._portfolio_manager.get_portfolio(strategy.strategy_id)
                if portfolio is None:
                    continue

                # Per-strategy consecutive-loss cooldown — sit out the next
                # tick window after N losing trades in a row.
                if self._loss_cooldown is not None:
                    on_cd, cd_reason = self._loss_cooldown.is_on_cooldown(strategy.strategy_id)
                    if on_cd:
                        logger.debug(
                            "[%s] cooldown active — skipping entries (%s)",
                            strategy.strategy_id, cd_reason,
                        )
                        continue

                open_positions: list[dict[str, Any]] = portfolio.get_open_positions()
                # Effective open count = persisted open + attempts so
                # far in this scan that haven't yet flushed to the
                # ``positions`` table (or were rejected and stamped
                # CLOSED).
                attempts: int = attempts_by_strategy.get(strategy.strategy_id, 0)
                if len(open_positions) + attempts >= strategy.get_max_positions():
                    continue

                # Don't double up on same ticker in same strategy
                if any(p["ticker"] == ticker for p in open_positions):
                    continue

                # 2026-04-29 incident dedup: rejected entries get stamped
                # CLOSED in the DB and the in-memory portfolio above
                # doesn't see them — so the next 5-min tick re-fires the
                # same order. Cross-check the DB for any same-day attempt
                # (open OR closed) by this strategy on this ticker.
                if self._already_attempted_today(ticker, strategy.strategy_id):
                    logger.debug(
                        "[%s] %s already attempted today — skipping",
                        strategy.strategy_id, ticker,
                    )
                    continue

                try:
                    decision: StrategyDecision | None = strategy.evaluate_entry(
                        ticker=ticker,
                        exchange="US",
                        df_5min=df_5min,
                        df_daily=df_daily,
                        current_price=current_price,
                        available_cash=portfolio.available_cash,
                        sentiment_score=sentiment_score,
                    )
                except Exception:
                    logger.warning(
                        "[%s] Entry evaluation error for %s", strategy.strategy_id, ticker,
                        exc_info=True,
                    )
                    continue

                if decision is None:
                    continue

                # Apply FOMC size multiplier (1.0 when not gated).
                if event_mult < 1.0:
                    decision = self._scale_decision_shares(decision, event_mult)
                    if decision is None:
                        continue

                # Per-symbol allocation cap across the global multi-strategy book.
                decision = self._enforce_symbol_cap(decision)
                if decision is None:
                    logger.debug(
                        "[%s] %s entry skipped — symbol allocation cap reached",
                        strategy.strategy_id, ticker,
                    )
                    continue

                om_decision: OMEntryDecision = self._build_om_decision(decision)

                # Reserve a slot BEFORE issuing the order so a slow/failed
                # broker call can't let the next ticker iteration over-fire.
                attempts_by_strategy[strategy.strategy_id] = attempts + 1

                if self._dry_run:
                    logger.info(
                        "[DRY RUN][%s] Would enter: %s %d shares @ $%.2f, stop=$%.2f",
                        strategy.strategy_id, ticker, decision.shares,
                        decision.entry_price, decision.stop_price,
                    )
                    continue

                trade_id: int | None = await self._order_manager.place_entry(om_decision)
                if trade_id is not None:
                    portfolio.record_entry(decision.shares, decision.entry_price)
                    entries_placed += 1
                    logger.info(
                        "[%s] Entry placed: %s trade_id=%d",
                        strategy.strategy_id, ticker, trade_id,
                    )

        return entries_placed

    async def check_exits(
        self,
        get_5min_bars: Any = None,
        get_daily_bars: Any = None,
    ) -> int:
        """Check exits for all strategies' positions. Returns exit count."""
        exits: int = 0

        for strategy in self._strategies:
            portfolio: VirtualPortfolio | None = self._portfolio_manager.get_portfolio(strategy.strategy_id)
            if portfolio is None:
                continue

            positions: list[dict[str, Any]] = portfolio.get_open_positions()
            for position in positions:
                ticker: str = position["ticker"]
                # Stale-data gate — same protection scan_for_entries uses.
                # A stale price that fires a false exit signal would
                # otherwise cascade into a phantom market sell, broken
                # virtual-portfolio cash, and a fake loss-cooldown record.
                if self._market_data.is_stale(ticker):
                    continue
                current_price: float | None = self._market_data.get_latest_price(ticker)
                if current_price is None or current_price <= 0:
                    continue

                df_5min: pd.DataFrame | None = None
                df_daily: pd.DataFrame | None = None
                if get_5min_bars:
                    try:
                        df_5min = await get_5min_bars(ticker, "US")
                    except Exception:
                        logger.warning("5min bar fetch failed for %s", ticker, exc_info=True)
                if get_daily_bars:
                    try:
                        df_daily = await get_daily_bars(ticker, "US")
                    except Exception:
                        logger.warning("Daily bar fetch failed for %s", ticker, exc_info=True)

                try:
                    exit_signal: ExitSignal = strategy.evaluate_exit(
                        position=position,
                        current_price=current_price,
                        df_5min=df_5min,
                        df_daily=df_daily,
                    )
                except Exception:
                    logger.warning(
                        "[%s] Exit evaluation error for %s", strategy.strategy_id, ticker,
                        exc_info=True,
                    )
                    continue

                if not exit_signal.should_exit:
                    continue

                logger.info(
                    "[%s] Exit signal for %s: %s (emergency=%s)",
                    strategy.strategy_id, ticker, exit_signal.reason, exit_signal.is_emergency,
                )

                if self._dry_run:
                    logger.info(
                        "[DRY RUN][%s] Would exit: %s @ $%.2f, reason=%s",
                        strategy.strategy_id, ticker, current_price, exit_signal.reason,
                    )
                    continue

                entry_price: float = float(position.get("entry_price", 0))
                shares: float = float(position.get("quantity", 0))
                if shares <= 0:
                    continue

                # CRITICAL: send the broker order *before* recording the
                # virtual exit. If the broker call fails we leave the
                # virtual portfolio untouched, so the next tick still
                # sees the position open and can re-attempt the exit
                # (rather than silently diverging from broker state).
                exit_order_id: str | None = await self._order_manager.place_exit(
                    ticker=ticker,
                    qty=shares,
                    reason=exit_signal.reason or "strategy_exit",
                    is_emergency=exit_signal.is_emergency,
                )
                if exit_order_id is None:
                    logger.error(
                        "[%s] Exit order rejected for %s — leaving virtual "
                        "portfolio untouched so we re-attempt next tick",
                        strategy.strategy_id, ticker,
                    )
                    continue

                portfolio.record_exit(shares, current_price, entry_price)
                exits += 1

                # Loss-cooldown bookkeeping — must follow record_exit so the
                # virtual portfolio's tally is consistent with the tracker's.
                if self._loss_cooldown is not None:
                    pnl: float = shares * (current_price - entry_price)
                    self._loss_cooldown.record_outcome(strategy.strategy_id, pnl)

        return exits

    def get_comparison_report(self) -> dict[str, dict[str, Any]]:
        return self._portfolio_manager.get_comparison_report()

    async def drain_disabled_sleeves(self) -> int:
        """Close positions whose strategy is no longer active.

        After a regime rebalance disables a sleeve, its open positions
        sit indefinitely — ``check_exits`` only iterates active strategies,
        so the broker-side stops are the only thing protecting them.
        That stranded SPY/breakout and XLRE/trend_following on
        2026-04-29. This routine flushes them on each tick (cheap when
        empty: a single SELECT).
        """
        active_ids: set[str] = {s.strategy_id for s in self._strategies}
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT * FROM positions WHERE status != 'CLOSED'"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            logger.warning("Drain orphan positions: DB read failed", exc_info=True)
            return 0

        closed: int = 0
        for row in rows:
            pos: dict[str, Any] = dict(row)
            sid: str | None = pos.get("strategy_id")
            # Treat both unknown sleeves and missing strategy_id as orphan.
            if sid in active_ids:
                continue
            ticker: str = pos["ticker"]
            qty: float = float(pos.get("quantity") or 0.0)
            if qty <= 0:
                continue

            logger.warning(
                "Draining orphan position: ticker=%s strategy=%s qty=%.4f",
                ticker, sid or "<none>", qty,
            )

            if self._dry_run:
                continue

            try:
                exit_order_id: str | None = await self._order_manager.place_exit(
                    ticker=ticker,
                    qty=qty,
                    reason=f"orphan_sleeve_drain:{sid or 'unknown'}",
                    is_emergency=False,
                )
            except Exception:
                logger.exception(
                    "Drain exit failed for %s (strategy=%s)", ticker, sid,
                )
                continue

            if exit_order_id is None:
                # Leave the row open so we retry next tick.
                continue
            closed += 1

        return closed

    def _build_om_decision(self, decision: StrategyDecision) -> OMEntryDecision:
        sector: str = GICS_SECTOR.get(decision.ticker, "Unknown")
        limit_price: float = self._clamp_limit_price(
            ticker=decision.ticker,
            side="BUY" if decision.direction == "long" else "SELL",
            requested_price=decision.entry_price,
        )
        return OMEntryDecision(
            ticker=decision.ticker,
            exchange=decision.exchange,
            side="BUY" if decision.direction == "long" else "SELL",
            shares=decision.shares,
            limit_price=limit_price,
            stop_price=decision.stop_price,
            target_price=decision.target_price or decision.entry_price * 1.05,
            hold_type=decision.hold_type.value,
            sector=sector,
            phase=self._config.get_phase().value,
            sentiment_score=decision.sentiment_score,
            signals=json.dumps(decision.signals) if decision.signals else "",
            currency="USD",
            strategy_id=decision.strategy_id,
            trail_pct=decision.trail_pct,
            trail_activation_price=decision.trail_activation_price,
        )

    # ------------------------------------------------------------------
    # Helpers — symbol cap, slop clamp, FOMC gate
    # ------------------------------------------------------------------

    def _already_attempted_today(self, ticker: str, strategy_id: str) -> bool:
        """Persistent same-day-attempt check (any status, any tick)."""
        et_today: str = datetime.now(tz=ET).date().isoformat()
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            try:
                return repo.has_attempted_today(
                    conn,
                    ticker=ticker,
                    strategy_id=strategy_id,
                    et_today_iso=et_today,
                )
            finally:
                conn.close()
        except Exception:
            # On DB error, fail OPEN (allow entry) — we have other guards
            # (in-memory portfolio dedup above, broker-side rejections).
            logger.warning(
                "Same-day attempt lookup failed for %s/%s — proceeding",
                strategy_id, ticker, exc_info=True,
            )
            return False

    def _fomc_size_multiplier(self) -> float:
        """Lookup today's FOMC multiplier from config (defaults to 1.0).

        Uses ``Config.raw_section()`` when available so we don't depend
        on the private ``_raw`` attribute. Falls back to ``_raw`` only
        for tests/mocks that pre-date the helper.
        """
        raw: dict[str, Any] = {}
        section_fn = getattr(self._config, "raw_section", None)
        if callable(section_fn):
            try:
                raw = section_fn()
            except Exception:
                raw = {}
        if not raw:
            legacy = getattr(self._config, "_raw", None)
            if isinstance(legacy, dict):
                raw = legacy
        try:
            today = datetime.now(tz=ET).date()
            return float(fomc_size_multiplier(today, raw))
        except Exception:
            logger.warning("FOMC size multiplier lookup failed", exc_info=True)
            return 1.0

    @staticmethod
    def _scale_decision_shares(
        decision: StrategyDecision, multiplier: float,
    ) -> StrategyDecision | None:
        """Return a new decision with shares scaled by *multiplier*, or None.

        Preserves integer-vs-fractional intent based on the original
        ``shares`` type. Returns a brand-new ``StrategyDecision`` so the
        caller's object is never mutated — composition with other helpers
        (FOMC × symbol cap) stays predictable regardless of call order.
        """
        if multiplier <= 0:
            return None
        scaled: float = float(decision.shares) * multiplier
        is_int_sized: bool = (
            isinstance(decision.shares, int) and not isinstance(decision.shares, bool)
        )
        if is_int_sized:
            scaled_int: int = int(scaled)
            if scaled_int < 1:
                return None
            return replace(decision, shares=scaled_int)
        scaled = round(scaled, 4)
        if scaled < 0.001:
            return None
        return replace(decision, shares=scaled)

    def _enforce_symbol_cap(
        self, decision: StrategyDecision,
    ) -> StrategyDecision | None:
        """Bound a candidate entry by its per-symbol allocation cap.

        Cap is expressed as fraction of the multi-strategy total book
        value. Existing exposure to ``decision.ticker`` across all
        sub-portfolios + the proposed entry must stay under the cap;
        otherwise the candidate is shrunk (or rejected if the resulting
        size would be below the strategy's min trade unit). Returns a
        new ``StrategyDecision`` rather than mutating the caller's
        object so transformations compose cleanly.
        """
        try:
            cap_pct: float = float(
                getattr(self._config, "get_symbol_max_allocation_pct", lambda _t: 1.0)(
                    decision.ticker,
                )
            )
        except (TypeError, ValueError):
            cap_pct = 1.0
        if cap_pct <= 0 or cap_pct >= 1.0:
            return decision

        total_book: float = self._compute_total_book_value()
        if total_book <= 0:
            return decision

        existing_exposure: float = self._compute_symbol_exposure(decision.ticker)
        cap_value: float = total_book * cap_pct
        remaining: float = max(cap_value - existing_exposure, 0.0)

        proposed_value: float = float(decision.shares) * float(decision.entry_price)
        if proposed_value <= remaining:
            return decision

        if remaining <= 0 or decision.entry_price <= 0:
            return None

        max_shares_by_cap: float = remaining / float(decision.entry_price)
        is_int_sized: bool = (
            isinstance(decision.shares, int) and not isinstance(decision.shares, bool)
        )
        if is_int_sized:
            shares_int: int = int(max_shares_by_cap)
            if shares_int < 1:
                return None
            new_decision: StrategyDecision = replace(decision, shares=shares_int)
        else:
            shares_frac: float = round(max_shares_by_cap, 4)
            if shares_frac < 0.001:
                return None
            new_decision = replace(decision, shares=shares_frac)

        logger.info(
            "[%s] %s shrunk by symbol cap %.0f%% (existing $%.2f, cap $%.2f, "
            "new shares=%s)",
            new_decision.strategy_id, new_decision.ticker, cap_pct * 100,
            existing_exposure, cap_value, new_decision.shares,
        )
        return new_decision

    def _compute_total_book_value(self) -> float:
        """Sum cash + open-position book value across all virtual portfolios."""
        total: float = 0.0
        try:
            portfolios = self._portfolio_manager.get_all_portfolios()
        except Exception:
            return 0.0
        for portfolio in portfolios.values():
            try:
                total += float(portfolio.current_cash)
            except Exception:
                continue
            try:
                positions = portfolio.get_open_positions()
            except Exception:
                positions = []
            for p in positions:
                try:
                    qty: float = float(p.get("quantity") or 0)
                    px: float = float(p.get("entry_price") or 0)
                    total += qty * px
                except Exception:
                    continue
        return total

    def _compute_symbol_exposure(self, ticker: str) -> float:
        """Sum (qty × entry_price) for *ticker* across all open positions."""
        exposure: float = 0.0
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT quantity, entry_price FROM positions "
                    "WHERE ticker = ? AND status != ?",
                    (ticker, PositionStatus.CLOSED.value),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return 0.0
        for r in rows:
            try:
                exposure += float(r["quantity"] or 0) * float(r["entry_price"] or 0)
            except Exception:
                continue
        return exposure

    def _clamp_limit_price(
        self, ticker: str, side: str, requested_price: float,
    ) -> float:
        """Bound an entry limit price to within ``entry.limit_slop_pct`` of NBBO.

        Buys: limit ≤ ask × (1 + slop). Sells: limit ≥ bid × (1 - slop).
        Falls back to the requested price when bid/ask isn't available.
        """
        try:
            slop: float = float(getattr(self._config, "entry_limit_slop_pct", 0.0))
        except (TypeError, ValueError):
            slop = 0.0
        if slop <= 0 or requested_price <= 0:
            return requested_price
        try:
            ba = self._market_data.get_bid_ask(ticker)
        except Exception:
            return requested_price
        if ba is None:
            return requested_price
        bid, ask = float(ba[0]), float(ba[1])
        if bid <= 0 or ask <= 0:
            return requested_price

        if side == "BUY":
            cap: float = ask * (1.0 + slop)
            if requested_price > cap:
                logger.info(
                    "%s BUY limit clamped: %.4f -> %.4f (ask=%.4f, slop=%.2f%%)",
                    ticker, requested_price, cap, ask, slop * 100,
                )
                return round(cap, 2)
            return requested_price

        floor: float = bid * (1.0 - slop)
        if requested_price < floor:
            logger.info(
                "%s SELL limit clamped: %.4f -> %.4f (bid=%.4f, slop=%.2f%%)",
                ticker, requested_price, floor, bid, slop * 100,
            )
            return round(floor, 2)
        return requested_price

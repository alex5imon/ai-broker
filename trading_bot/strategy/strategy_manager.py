"""Strategy manager — orchestrates multiple strategies in the main loop."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.constants import GICS_SECTOR, HoldType
from trading_bot.execution.order_manager import EntryDecision as OMEntryDecision
from trading_bot.execution.virtual_portfolio import PortfolioManager, VirtualPortfolio
from trading_bot.strategy.base import ExitSignal, StrategyBase, StrategyDecision
from trading_bot.strategy.regime_filter import RegimeFilter

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = ZoneInfo("US/Eastern")


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

    @property
    def strategies(self) -> list[StrategyBase]:
        return list(self._strategies)

    async def scan_for_entries(
        self,
        watchlist: list[str],
        get_5min_bars: Any,
        get_daily_bars: Any,
        account_equity_gbp: float,
    ) -> int:
        """Run all strategies against the watchlist. Returns number of entries placed."""
        entries_placed: int = 0

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

                open_positions: list[dict[str, Any]] = portfolio.get_open_positions()
                if len(open_positions) >= strategy.get_max_positions():
                    continue

                # Don't double up on same ticker in same strategy
                if any(p["ticker"] == ticker for p in open_positions):
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

                om_decision: OMEntryDecision = self._build_om_decision(decision)

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
                shares: int = int(position.get("quantity", 0))
                portfolio.record_exit(shares, current_price, entry_price)
                exits += 1

        return exits

    def get_comparison_report(self) -> dict[str, dict[str, Any]]:
        return self._portfolio_manager.get_comparison_report()

    def _build_om_decision(self, decision: StrategyDecision) -> OMEntryDecision:
        sector: str = GICS_SECTOR.get(decision.ticker, "Unknown")
        return OMEntryDecision(
            ticker=decision.ticker,
            exchange=decision.exchange,
            side="BUY" if decision.direction == "long" else "SELL",
            shares=decision.shares,
            limit_price=decision.entry_price,
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

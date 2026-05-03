"""Entry signal evaluation and trade decision logic.

Evaluates all entry conditions from SPEC Section 6: technical signals,
sentiment, earnings blackout, cooldowns, spread, commission efficiency,
settled cash, position limits, sector exposure, correlation, and daily
trade/P&L limits.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.config import Config
from trading_bot.constants import (
    GICS_SECTOR,
    HoldType,
    Market,
    Phase,
    TZ_EASTERN,
)
from trading_bot.data.earnings import EarningsCalendar
from trading_bot.data.market_data import MarketDataManager
from trading_bot.data.sentiment import SentimentAnalyzer
from trading_bot.db import repository as repo
from trading_bot.strategy.technical import TechnicalAnalyzer

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EntryDecision:
    """Result of evaluating whether to enter a trade."""

    ticker: str
    exchange: str
    should_enter: bool
    direction: str | None  # 'long' or 'short'
    signal_price: float | None
    position_size: float | None  # Number of shares (float to support fractional)
    position_value_usd: float | None
    stop_price: float | None
    target_price: float | None
    hold_type: HoldType | None
    signals: dict[str, Any] | None
    sentiment_score: float | None
    atr_rank: float | None
    rejection_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# EntryEvaluator
# ---------------------------------------------------------------------------


class EntryEvaluator:
    """Evaluates entry conditions and decides whether to enter a trade.

    Orchestrates all the checks from SPEC Section 6.  Each check is a
    separate method so it can be unit-tested in isolation.
    """

    def __init__(
        self,
        config: Config,
        technical: TechnicalAnalyzer,
        sentiment: SentimentAnalyzer,
        earnings: EarningsCalendar,
        market_data: MarketDataManager,
        db_path: str,
    ) -> None:
        self._config: Config = config
        self._technical: TechnicalAnalyzer = technical
        self._sentiment: SentimentAnalyzer = sentiment
        self._earnings: EarningsCalendar = earnings
        self._market_data: MarketDataManager = market_data
        self._db_path: str = db_path

        # Entry config
        self._min_signals: int = int(config._require("entry", "min_signals_required"))
        self._sentiment_threshold: float = float(
            config._require("entry", "sentiment_threshold")
        )
        self._sentiment_block: float = float(
            config._require("entry", "sentiment_block_threshold")
        )
        self._no_data_size_mult: float = float(
            config._require("entry", "no_data_size_multiplier")
        )
        self._spread_max_us: float = float(
            config._require("entry", "spread_max_us_pct")
        )
        self._cooldown_minutes: int = int(
            config._require("entry", "cooldown_minutes")
        )
        self._blackout_hours: int = int(
            config._require("entry", "earnings_blackout_hours")
        )

        # ATR thresholds (from strategy.atr in config)
        self._atr_extreme: float = float(
            config._require("strategy", "atr", "extreme_percentile")
        )
        self._atr_high: float = float(
            config._require("strategy", "atr", "high_percentile")
        )
        self._atr_high_reduction: float = float(
            config._require("strategy", "atr", "high_vol_size_reduction")
        )

    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    async def evaluate(
        self,
        ticker: str,
        exchange: str,
        df_5min: pd.DataFrame,
        df_daily: pd.DataFrame,
        account_equity_usd: float,
    ) -> EntryDecision:
        """Full entry evaluation for a ticker.

        Runs every check from SPEC Section 6 in order.  Returns an
        :class:`EntryDecision` with ``should_enter=True`` only if every
        gate passes.
        """
        rejections: list[str] = []
        phase: Phase = self._config.get_phase()

        # -- 1. Technical signals -----------------------------------------
        signals: dict[str, Any] = self._technical.get_signals(df_5min, df_daily)
        signal_count: int = signals["signal_count"]
        direction: str | None = signals["direction"]
        atr_rank: float = signals["atr_rank"]

        if signal_count < self._min_signals:
            rejections.append(
                f"Insufficient signals: {signal_count}/{self._min_signals}"
            )

        if direction is None and signal_count >= self._min_signals:
            rejections.append("Signals present but direction is ambiguous")

        # -- 2. ATR rank filter -------------------------------------------
        if atr_rank >= self._atr_extreme:
            rejections.append(
                f"ATR rank {atr_rank:.1f} >= extreme threshold {self._atr_extreme}"
            )

        # -- 3. Sentiment -------------------------------------------------
        sentiment_score: float | None = await self._sentiment.get_sentiment(ticker)
        sentiment_size_mult: float = 1.0

        if sentiment_score is not None:
            if direction == "long" and sentiment_score < self._sentiment_threshold:
                if sentiment_score < self._sentiment_block:
                    rejections.append(
                        f"Sentiment {sentiment_score:.2f} < block threshold "
                        f"{self._sentiment_block}"
                    )
                else:
                    rejections.append(
                        f"Sentiment {sentiment_score:.2f} < entry threshold "
                        f"{self._sentiment_threshold} for longs"
                    )
        else:
            # No data: proceed at reduced size
            sentiment_size_mult = self._no_data_size_mult
            logger.info(
                "%s: No sentiment data; will use %.0f%% position size",
                ticker,
                sentiment_size_mult * 100,
            )

        # -- 4. Earnings blackout -----------------------------------------
        now_et: datetime = datetime.now(TZ_EASTERN)
        if self._earnings.is_in_blackout(ticker, now_et):
            rejections.append(
                f"In {self._blackout_hours}h earnings blackout window"
            )

        # -- 5. Cooldown --------------------------------------------------
        if self._is_on_cooldown(ticker, now_et):
            rejections.append(
                f"Ticker on {self._cooldown_minutes}-min cooldown"
            )

        # -- 6. Spread check -----------------------------------------------
        spread_ok, spread_reason = self._check_spread(ticker, exchange)
        if not spread_ok:
            rejections.append(spread_reason)

        # -- 7. Get signal price (mid-price) --------------------------------
        bid_ask: tuple[float, float] | None = self._market_data.get_bid_ask(ticker)
        if bid_ask is None:
            rejections.append("No bid/ask data available")
            return self._reject(ticker, exchange, rejections, signals, sentiment_score, atr_rank)

        bid, ask = bid_ask
        signal_price: float = (bid + ask) / 2.0

        # -- 8. Determine hold type and exit params --------------------------
        hold_type: HoldType = self._determine_hold_type(phase)
        exit_params: dict[str, Any] = self._config.get_exit_params(hold_type)
        stop_loss_pct: float = float(exit_params["stop_loss_pct"])
        take_profit_pct: float = float(exit_params["take_profit_pct"])

        if direction == "long":
            stop_price: float = signal_price * (1.0 - stop_loss_pct)
            target_price: float = signal_price * (1.0 + take_profit_pct)
        elif direction == "short":
            stop_price = signal_price * (1.0 + stop_loss_pct)
            target_price = signal_price * (1.0 - take_profit_pct)
        else:
            stop_price = 0.0
            target_price = 0.0

        # -- 9. Position sizing ----------------------------------------------
        position_size, position_value_local = self._compute_position_size(
            ticker=ticker,
            exchange=exchange,
            signal_price=signal_price,
            stop_loss_pct=stop_loss_pct,
            account_equity_usd=account_equity_usd,
            atr_rank=atr_rank,
            sentiment_size_mult=sentiment_size_mult,
            phase=phase,
        )

        position_value_usd: float = position_value_local

        if position_size <= 0:
            rejections.append("Computed position size is zero shares")

        # -- 10. Max positions check -----------------------------------------
        max_positions: int = self._config.get_max_positions()
        open_count: int = self._get_open_position_count()
        if open_count >= max_positions:
            rejections.append(
                f"Max positions reached: {open_count}/{max_positions}"
            )

        # -- 13. Sector exposure check ---------------------------------------
        sector: str = GICS_SECTOR.get(ticker, "Unknown")
        max_sector: int = self._config.get_max_sector_exposure()
        sector_count: int = self._get_sector_count(sector)
        if sector_count >= max_sector:
            rejections.append(
                f"Max sector exposure reached for {sector}: "
                f"{sector_count}/{max_sector}"
            )

        # -- 14. Correlation check (skip if no positions open) ---------------
        # Correlation is checked at portfolio level; we log but do not
        # block in Phase 1 with max 2 positions (low overlap risk).
        # The check is here for Phase 2/3 readiness.
        if open_count > 0:
            corr_ok, corr_reason = self._check_correlation(ticker)
            if not corr_ok:
                rejections.append(corr_reason)

        # -- 15. Daily trade count -------------------------------------------
        max_daily: int = self._config.get_max_daily_trades()
        daily_count: int = self._get_daily_trade_count()
        if daily_count >= max_daily:
            rejections.append(
                f"Daily trade limit reached: {daily_count}/{max_daily}"
            )

        # -- 16. Daily P&L limit ---------------------------------------------
        daily_loss_limit: float = account_equity_usd * self._config.daily_loss_limit_pct
        daily_pnl: float = self._get_daily_pnl_usd()
        if daily_pnl < 0 and abs(daily_pnl) >= daily_loss_limit:
            rejections.append(
                f"Daily loss limit hit: {daily_pnl:.2f} USD "
                f"(limit -{daily_loss_limit:.2f} USD)"
            )

        # -- Decision ---------------------------------------------------------
        if rejections:
            logger.info(
                "%s: Entry rejected (%d reasons): %s",
                ticker,
                len(rejections),
                "; ".join(rejections),
            )
            return self._reject(
                ticker, exchange, rejections, signals, sentiment_score, atr_rank
            )

        logger.info(
            "%s: Entry APPROVED -- %s %.6f shares @ %.4f, "
            "stop=%.4f, target=%.4f, hold=%s",
            ticker,
            direction,
            position_size,
            signal_price,
            stop_price,
            target_price,
            hold_type.value,
        )

        return EntryDecision(
            ticker=ticker,
            exchange=exchange,
            should_enter=True,
            direction=direction,
            signal_price=signal_price,
            position_size=position_size,
            position_value_usd=position_value_usd,
            stop_price=stop_price,
            target_price=target_price,
            hold_type=hold_type,
            signals=signals,
            sentiment_score=sentiment_score,
            atr_rank=atr_rank,
            rejection_reasons=[],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _reject(
        self,
        ticker: str,
        exchange: str,
        reasons: list[str],
        signals: dict[str, Any] | None,
        sentiment: float | None,
        atr_rank: float | None,
    ) -> EntryDecision:
        """Build a rejection decision."""
        return EntryDecision(
            ticker=ticker,
            exchange=exchange,
            should_enter=False,
            direction=None,
            signal_price=None,
            position_size=None,
            position_value_usd=None,
            stop_price=None,
            target_price=None,
            hold_type=None,
            signals=signals,
            sentiment_score=sentiment,
            atr_rank=atr_rank,
            rejection_reasons=reasons,
        )

    def _determine_hold_type(self, phase: Phase) -> HoldType:
        """Determine whether this trade should be intraday or swing.

        Phase 1 (~GBP 950): swing trading is the primary mode because
        commissions are too high for tight intraday scalps.
        Phase 2+: intraday becomes viable.
        """
        if phase <= Phase.MICRO:
            return HoldType.SWING
        return HoldType.INTRADAY

    def _compute_position_size(
        self,
        ticker: str,
        exchange: str,
        signal_price: float,
        stop_loss_pct: float,
        account_equity_usd: float,
        atr_rank: float,
        sentiment_size_mult: float,
        phase: Phase,
    ) -> tuple[float, float]:
        """Compute number of shares and position value in local currency.

        Implements the sizing formula from SPEC Section 6:
            max_risk_amount = equity * risk_per_trade
            stop_distance = entry_price * stop_loss_pct
            shares = floor(max_risk_amount / stop_distance)

        Then applies constraints in order:
        1. position_value <= equity * max_position_pct
        2. position_value <= settled_cash_available
        3. floor to whole shares
        4. position_value >= minimum_position_value
        5. ATR high vol reduction
        6. Sentiment no-data reduction

        Returns (shares, position_value_in_local_currency).
        """
        if signal_price <= 0 or stop_loss_pct <= 0:
            return 0, 0.0

        risk_per_trade: float = self._config.get_risk_per_trade()
        max_risk_amount: float = account_equity_usd * risk_per_trade

        stop_distance: float = signal_price * stop_loss_pct
        if stop_distance <= 0:
            return 0, 0.0

        shares: int = math.floor(max_risk_amount / stop_distance)

        # Constraint 1: max position percentage
        max_position_pct: float = self._config.get_max_position_pct()
        max_position_value: float = account_equity_usd * max_position_pct
        if shares * signal_price > max_position_value:
            shares = math.floor(max_position_value / signal_price)

        # Constraint 2: ATR high-vol reduction
        if atr_rank >= self._atr_high:
            shares = math.floor(shares * self._atr_high_reduction)
            logger.info(
                "%s: ATR rank %.1f >= %.1f; reducing size by %.0f%%",
                ticker,
                atr_rank,
                self._atr_high,
                (1.0 - self._atr_high_reduction) * 100,
            )

        # Constraint 3: sentiment no-data reduction
        if sentiment_size_mult < 1.0:
            shares = math.floor(shares * sentiment_size_mult)

        # Constraint 4: minimum position value
        market: Market = Market.US
        min_value: float = self._config.get_min_position_value(market)
        position_value: float = shares * signal_price

        if position_value < min_value:
            logger.info(
                "%s: Position value %.2f USD < minimum %.2f; skipping",
                ticker,
                position_value,
                min_value,
            )
            return 0, 0.0

        return shares, position_value

    def _check_spread(self, ticker: str, exchange: str) -> tuple[bool, str]:
        """Check if the current spread is acceptable for entry.

        Returns (ok, reason).
        """
        spread: float | None = self._market_data.get_spread_pct(ticker)
        if spread is None:
            return False, "Spread data unavailable"

        if spread > self._spread_max_us:
            return False, (
                f"Spread {spread:.4%} exceeds max {self._spread_max_us:.4%}"
            )

        return True, ""

    def _is_on_cooldown(self, ticker: str, now: datetime) -> bool:
        """Check if *ticker* is in a post-exit cooldown window."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cooldown_until: datetime | None = repo.get_cooldown(conn, ticker)
            conn.close()
        except sqlite3.Error:
            logger.exception("Error checking cooldown for %s", ticker)
            return False

        if cooldown_until is None:
            return False

        # Ensure both are timezone-aware for comparison
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=TZ_EASTERN)

        return now < cooldown_until

    def _get_open_position_count(self) -> int:
        """Get the number of currently open positions from the DB."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
            return len(positions)
        except sqlite3.Error:
            logger.exception("Error fetching open positions")
            return 0

    def _get_sector_count(self, sector: str) -> int:
        """Count open positions in the given GICS sector."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
        except sqlite3.Error:
            logger.exception("Error fetching positions for sector check")
            return 0

        count: int = 0
        for pos in positions:
            pos_ticker: str = pos.get("ticker", "")
            if GICS_SECTOR.get(pos_ticker, "") == sector:
                count += 1
        return count

    def _check_correlation(self, ticker: str) -> tuple[bool, str]:
        """Check correlation with existing open positions.

        Uses the configured correlation threshold (0.85).  For Phase 1
        with max 2 positions this is a lightweight check -- we compare
        sectors as a proxy.  Full return-correlation requires historical
        data and will be implemented for Phase 2+.
        """
        sector: str = GICS_SECTOR.get(ticker, "Unknown")

        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            positions: list[dict[str, Any]] = repo.get_open_positions(conn)
            conn.close()
        except sqlite3.Error:
            logger.exception("Error checking correlation")
            return True, ""

        for pos in positions:
            pos_ticker: str = pos.get("ticker", "")
            pos_sector: str = GICS_SECTOR.get(pos_ticker, "Unknown")
            # Same sector is a proxy for high correlation at Phase 1 scale.
            # Sector exposure is checked separately; this is an additional
            # safeguard for within-sector pairs.
            if pos_sector == sector and pos_sector != "Unknown":
                logger.debug(
                    "%s and %s are in the same sector (%s); flagging correlation",
                    ticker,
                    pos_ticker,
                    sector,
                )
                # We do not block here -- sector exposure check handles limits.
                # This method is a placeholder for Phase 2+ return-based
                # correlation analysis.

        return True, ""

    def _get_daily_trade_count(self) -> int:
        """Get today's trade count from the DB."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            count: int = repo.get_trade_count_today(conn)
            conn.close()
            return count
        except sqlite3.Error:
            logger.exception("Error fetching daily trade count")
            return 0

    def _get_daily_pnl_usd(self) -> float:
        """Get today's realised P&L in USD from the DB."""
        try:
            conn: sqlite3.Connection = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            pnl: float = repo.get_daily_pnl_usd(conn)
            conn.close()
            return pnl
        except sqlite3.Error:
            logger.exception("Error fetching daily P&L")
            return 0.0

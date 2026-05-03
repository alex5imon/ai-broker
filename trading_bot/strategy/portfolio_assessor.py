"""Phase 0 portfolio assessment and cleanup.

Scores each existing position (0-100) across liquidity, market cap,
exchange quality, technical health, sentiment, and loss magnitude.
Classifies as HOLD, SELL, or URGENT_SELL and executes the cleanup plan
with ntfy notification and progressive order adjustment.

See SPEC Section 3 for the full specification.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pandas as pd

from trading_bot.config import Config
from trading_bot.constants import (
    TZ_EASTERN,
)
from trading_bot.data.market_data import MarketDataManager
from trading_bot.data.sentiment import SentimentAnalyzer

if TYPE_CHECKING:
    from trading_bot.gateway.connection import GatewayConnection
    from trading_bot.notifications.notifier import Notifier

logger: logging.Logger = logging.getLogger(__name__)

ET: ZoneInfo = TZ_EASTERN


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PositionAssessment:
    """Result of scoring a single position."""

    ticker: str
    exchange: str
    current_value_usd: float
    unrealized_pnl_usd: float
    score: int  # 0-100
    classification: str  # 'HOLD', 'SELL', 'URGENT_SELL'
    scores_breakdown: dict[str, int] = field(default_factory=dict)
    reasoning: str = ""
    recommended_action: str = ""
    trailing_stop_price: float | None = None


# ---------------------------------------------------------------------------
# PortfolioAssessor
# ---------------------------------------------------------------------------


class PortfolioAssessor:
    """Assesses existing positions for Phase 0 cleanup.

    Scoring weights (from config ``phase0.scoring``):
    - Liquidity: 25
    - Market Cap: 20
    - Exchange Quality: 15
    - Technical Health: 15
    - Sentiment: 10
    - Loss Magnitude: 15
    """

    def __init__(
        self,
        config: Config,
        market_data: MarketDataManager,
        sentiment: SentimentAnalyzer,
        notifier: Notifier,
    ) -> None:
        self._config: Config = config
        self._market_data: MarketDataManager = market_data
        self._sentiment: SentimentAnalyzer = sentiment
        self._notifier: Notifier = notifier

        # Scoring weights from config
        scoring_cfg: dict[str, Any] = config._get("phase0", "scoring") or {}
        self._w_liquidity: int = int(scoring_cfg.get("liquidity_weight", 25))
        self._w_market_cap: int = int(scoring_cfg.get("market_cap_weight", 20))
        self._w_exchange: int = int(scoring_cfg.get("exchange_weight", 15))
        self._w_technical: int = int(scoring_cfg.get("technical_weight", 15))
        self._w_sentiment: int = int(scoring_cfg.get("sentiment_weight", 10))
        self._w_loss: int = int(scoring_cfg.get("loss_weight", 15))

        # Thresholds
        thresholds_cfg: dict[str, Any] = config._get("phase0", "thresholds") or {}
        self._hold_min_score: int = int(thresholds_cfg.get("hold_min_score", 60))
        self._sell_min_score: int = int(thresholds_cfg.get("sell_min_score", 30))

        # Sell strategy
        sell_cfg: dict[str, Any] = config._get("phase0", "sell_strategy") or {}
        self._adjust_toward_bid_hours: int = int(
            sell_cfg.get("adjust_toward_bid_after_hours", 2)
        )
        self._adjust_to_bid_hours: int = int(
            sell_cfg.get("adjust_to_bid_after_hours", 4)
        )
        self._urgent_adjust_minutes: int = int(
            sell_cfg.get("urgent_adjust_interval_minutes", 30)
        )
        self._market_order_threshold: float = float(
            sell_cfg.get("market_order_threshold_value", 50)
        )

        # Notification delay
        self._notification_delay_s: int = int(
            config._get("phase0", "notification_delay_seconds") or 300
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def assess_portfolio(
        self, positions: list[dict[str, Any]]
    ) -> list[PositionAssessment]:
        """Score every position and classify as HOLD, SELL, or URGENT_SELL.

        Args:
            positions: List of position dicts with keys: ``ticker``,
                ``exchange``, ``quantity``, ``avg_cost``, ``market_value``,
                ``unrealized_pnl``, ``currency``.

        Returns:
            Sorted list of :class:`PositionAssessment` (URGENT_SELL first).
        """
        assessments: list[PositionAssessment] = []
        for pos in positions:
            assessment: PositionAssessment = await self.score_position(pos)
            assessments.append(assessment)

        # Sort: lowest score first (URGENT_SELL -> SELL -> HOLD)
        assessments.sort(key=lambda a: a.score)

        for a in assessments:
            logger.info(
                "Phase 0 assessment: %s (score=%d, class=%s) -- %s",
                a.ticker,
                a.score,
                a.classification,
                a.reasoning,
            )

        return assessments

    async def score_position(
        self, position: dict[str, Any]
    ) -> PositionAssessment:
        """Score a single position (0-100) across all criteria.

        See SPEC Section 3 for the scoring rubric.
        """
        ticker: str = position.get("ticker", "")
        exchange: str = position.get("exchange", "OTC")
        currency: str = position.get("currency", "USD")
        market_value: float = float(position.get("market_value", 0.0))
        unrealized_pnl: float = float(position.get("unrealized_pnl", 0.0))
        avg_cost: float = float(position.get("avg_cost", 0.0))
        # Float-typed: positions.quantity is fractional under the
        # ai-broker#39 entry path. int() truncation of 0.43 → 0 would zero
        # out cost_basis below and produce a 0% P&L for any sub-1-share
        # holding.
        quantity: float = float(position.get("quantity", 0))

        # Account is USD-only — Alpaca returns USD, no conversion needed
        value_usd: float = market_value
        pnl_usd: float = unrealized_pnl

        # P&L percentage
        cost_basis: float = avg_cost * abs(quantity) if avg_cost > 0 else 0.0
        pnl_pct: float = (unrealized_pnl / cost_basis) if cost_basis > 0 else 0.0

        # Compute individual scores
        liquidity_score: int = await self._score_liquidity(ticker, exchange)
        market_cap_score: int = await self._score_market_cap(ticker, exchange)
        exchange_score: int = self._score_exchange(exchange)
        technical_score: int = await self._score_technical(ticker, exchange)
        sentiment_score: int = await self._score_sentiment(ticker)
        loss_score: int = self._score_loss(pnl_pct)

        # Weighted total
        total: int = (
            liquidity_score
            + market_cap_score
            + exchange_score
            + technical_score
            + sentiment_score
            + loss_score
        )

        # Clamp to 0-100
        total = max(0, min(100, total))

        # Classify
        if total >= self._hold_min_score:
            classification: str = "HOLD"
        elif total >= self._sell_min_score:
            classification = "SELL"
        else:
            classification = "URGENT_SELL"

        # Build breakdown
        breakdown: dict[str, int] = {
            "liquidity": liquidity_score,
            "market_cap": market_cap_score,
            "exchange": exchange_score,
            "technical": technical_score,
            "sentiment": sentiment_score,
            "loss": loss_score,
        }

        # Build reasoning string
        reasoning: str = self._build_reasoning(
            ticker, classification, breakdown, pnl_pct, exchange
        )

        # Recommended action
        recommended: str = self._build_recommendation(
            classification, ticker, market_value, currency
        )

        # Trailing stop for HOLD positions (-5% from current price)
        trailing_stop: float | None = None
        if classification == "HOLD" and quantity > 0:
            current_price: float | None = self._market_data.get_latest_price(ticker)
            if current_price is not None and current_price > 0:
                trailing_stop = current_price * 0.95

        return PositionAssessment(
            ticker=ticker,
            exchange=exchange,
            current_value_usd=value_usd,
            unrealized_pnl_usd=pnl_usd,
            score=total,
            classification=classification,
            scores_breakdown=breakdown,
            reasoning=reasoning,
            recommended_action=recommended,
            trailing_stop_price=trailing_stop,
        )

    async def execute_cleanup(
        self,
        assessments: list[PositionAssessment],
        gateway: GatewayConnection,
    ) -> list[dict[str, Any]]:
        """Execute the cleanup plan: notify, wait, sell, monitor.

        1. Send ntfy notification with full plan
        2. Wait ``notification_delay_seconds`` (5 min)
        3. Execute sells (URGENT_SELL first, then SELL)
        4. Place trailing stops on HOLD positions
        5. Monitor fills and adjust orders progressively

        Returns a list of execution result dicts.
        """
        # Build notification message
        plan_lines: list[str] = ["PORTFOLIO CLEANUP PLAN", ""]
        holds: list[PositionAssessment] = []
        sells: list[PositionAssessment] = []

        for a in assessments:
            if a.classification == "HOLD":
                holds.append(a)
                trail_str: str = (
                    f"trailing stop at {a.trailing_stop_price:.2f}"
                    if a.trailing_stop_price is not None
                    else "no trailing stop"
                )
                plan_lines.append(
                    f"HOLD: {a.ticker} (score {a.score}) - {trail_str}"
                )
            else:
                sells.append(a)
                plan_lines.append(
                    f"{a.classification}: {a.ticker} (score {a.score}) - "
                    f"{a.recommended_action}"
                )

        plan_lines.append("")
        plan_lines.append(
            f"Executing in {self._notification_delay_s // 60} minutes..."
        )

        message: str = "\n".join(plan_lines)

        # Send notification
        await self._notifier.send(
            title="Phase 0: Portfolio Cleanup",
            message=message,
            priority=4,
            tags=["warning", "chart_with_downwards_trend"],
        )

        logger.info(
            "Phase 0 plan sent via ntfy; waiting %d seconds before executing",
            self._notification_delay_s,
        )

        # Wait for notification delay (allows kill switch)
        await asyncio.sleep(self._notification_delay_s)

        results: list[dict[str, Any]] = []

        # Execute sells: URGENT_SELL first (already sorted by score, lowest first)
        for assessment in sells:
            result: dict[str, Any] = await self._execute_sell(assessment, gateway)
            results.append(result)

        # Place trailing stops on HOLD positions
        for assessment in holds:
            if assessment.trailing_stop_price is not None:
                result = await self._place_trailing_stop(assessment, gateway)
                results.append(result)

        # Send completion notification
        sell_count: int = len(sells)
        hold_count: int = len(holds)
        await self._notifier.send(
            title="Phase 0: Cleanup Complete",
            message=(
                f"Sold/selling: {sell_count} positions\n"
                f"Holding: {hold_count} positions with trailing stops\n"
                f"See logs for full details."
            ),
            priority=3,
            tags=["white_check_mark"],
        )

        return results

    # ------------------------------------------------------------------
    # Scoring methods
    # ------------------------------------------------------------------

    async def _score_liquidity(self, ticker: str, exchange: str) -> int:
        """Score liquidity (0 to weight max, default 25).

        Uses average daily volume from historical data.
        >1M shares = max, >100K = 15, >10K = 8, <10K = 0.
        """
        try:
            bars: list[Any] = await self._market_data.get_historical_bars(
                ticker, exchange, bar_size="1 day", duration="20 D"
            )
        except Exception:
            logger.exception("Failed to fetch historical bars for %s", ticker)
            return 0

        if not bars:
            logger.debug("%s: No historical bars for liquidity scoring", ticker)
            return 0

        volumes: list[float] = [
            float(getattr(b, "volume", 0)) for b in bars if hasattr(b, "volume")
        ]
        if not volumes:
            return 0

        avg_volume: float = sum(volumes) / len(volumes)

        if avg_volume > 1_000_000:
            return self._w_liquidity
        if avg_volume > 100_000:
            return int(self._w_liquidity * 0.60)  # 15/25
        if avg_volume > 10_000:
            return int(self._w_liquidity * 0.32)  # 8/25
        return 0

    async def _score_market_cap(self, ticker: str, exchange: str) -> int:
        """Score market cap (0 to weight max, default 20).

        Attempts IB fundamentals; falls back to heuristic from price * volume.
        >$10B = max, >$1B = 15, >$500M = 10, >$100M = 5, <$100M = 0.

        Existing positions get a relaxed threshold: $100M instead of $500M
        (per SPEC Section 3 note).
        """
        # Heuristic: use price * avg_volume * 252 as a very rough proxy
        # (actual shares outstanding unknown without fundamentals data)
        price: float | None = self._market_data.get_latest_price(ticker)
        if price is None or price <= 0:
            return 0

        try:
            bars: list[Any] = await self._market_data.get_historical_bars(
                ticker, exchange, bar_size="1 day", duration="20 D"
            )
        except Exception:
            logger.exception(
                "Failed to fetch bars for market cap heuristic: %s", ticker
            )
            return 0

        if not bars:
            return 0

        volumes: list[float] = [
            float(getattr(b, "volume", 0)) for b in bars if hasattr(b, "volume")
        ]
        avg_volume: float = sum(volumes) / len(volumes) if volumes else 0

        # Very rough market-cap heuristic -- heavily discounted
        # A real implementation would use IB reqFundamentalData
        estimated_cap: float = price * avg_volume * 252 * 0.01

        currency: str = "USD"

        # Score (relaxed thresholds for existing positions per SPEC)
        if estimated_cap > 10_000_000_000:
            return self._w_market_cap  # 20
        if estimated_cap > 1_000_000_000:
            return int(self._w_market_cap * 0.75)  # 15
        if estimated_cap > 500_000_000:
            return int(self._w_market_cap * 0.50)  # 10
        if estimated_cap > 100_000_000:
            return int(self._w_market_cap * 0.25)  # 5
        return 0

    def _score_exchange(self, exchange: str) -> int:
        """Score exchange quality (0 to weight max, default 15).

        NYSE / NASDAQ = max, OTC = 0.
        """
        exchange_upper: str = exchange.upper()
        if exchange_upper in ("NYSE", "NASDAQ", "US"):
            return self._w_exchange  # 15
        return 0  # OTC

    async def _score_technical(self, ticker: str, exchange: str) -> int:
        """Score technical health (0 to weight max, default 15).

        Price vs 50-day SMA: above = 10, within 5% below = 5, far below = 0.
        RSI 30-70 = 5, outside = 0.
        """
        try:
            bars: list[Any] = await self._market_data.get_historical_bars(
                ticker, exchange, bar_size="1 day", duration="100 D"
            )
        except Exception:
            logger.exception("Failed to fetch bars for technical scoring: %s", ticker)
            return 0

        if len(bars) < 50:
            logger.debug(
                "%s: Only %d daily bars; insufficient for technical scoring",
                ticker,
                len(bars),
            )
            return 0

        # Build DataFrame
        data: list[dict[str, Any]] = []
        for b in bars:
            data.append(
                {
                    "close": float(getattr(b, "close", 0)),
                    "high": float(getattr(b, "high", 0)),
                    "low": float(getattr(b, "low", 0)),
                }
            )
        df: pd.DataFrame = pd.DataFrame(data)

        # 50-day SMA
        sma50: pd.Series = df["close"].rolling(window=50).mean()
        current_price: float = df["close"].iloc[-1]
        current_sma: float = float(sma50.iloc[-1])

        sma_score: int = 0
        if current_sma > 0:
            pct_from_sma: float = (current_price - current_sma) / current_sma
            if pct_from_sma >= 0:
                sma_score = 10
            elif pct_from_sma >= -0.05:
                sma_score = 5
            # else 0

        # RSI — pure pandas: Wilder's smoothed RS
        rsi_score: int = 0
        delta: pd.Series = df["close"].diff()
        gain: pd.Series = delta.clip(lower=0)
        loss: pd.Series = (-delta).clip(lower=0)
        avg_gain: pd.Series = gain.ewm(alpha=1.0 / 14, adjust=False).mean()
        avg_loss: pd.Series = loss.ewm(alpha=1.0 / 14, adjust=False).mean()
        rs: pd.Series = avg_gain / avg_loss.replace(0, float("nan"))
        rsi_series: pd.Series = 100.0 - (100.0 / (1.0 + rs))
        if not rsi_series.empty:
            current_rsi: float = float(rsi_series.iloc[-1])
            if not math.isnan(current_rsi) and 30 <= current_rsi <= 70:
                rsi_score = 5

        return sma_score + rsi_score

    async def _score_sentiment(self, ticker: str) -> int:
        """Score sentiment (0 to weight max, default 10).

        > 0.1 = max, neutral (around 0) = 5, negative = 0.
        """
        score_val: float | None = await self._sentiment.get_sentiment(ticker)

        if score_val is None:
            # No data -- treat as neutral
            return int(self._w_sentiment * 0.5)  # 5

        if score_val > 0.1:
            return self._w_sentiment  # 10
        if score_val >= -0.1:
            return int(self._w_sentiment * 0.5)  # 5
        return 0  # Negative

    def _score_loss(self, pnl_pct: float) -> int:
        """Score based on unrealized loss magnitude (0 to weight max, default 15).

        Profit or < -5% = max, -5% to -15% = 10, -15% to -30% = 5, > -30% = 0.
        """
        if pnl_pct >= -0.05:
            return self._w_loss  # 15
        if pnl_pct >= -0.15:
            return int(self._w_loss * 0.67)  # 10
        if pnl_pct >= -0.30:
            return int(self._w_loss * 0.33)  # 5
        return 0

    # ------------------------------------------------------------------
    # Reasoning and recommendations
    # ------------------------------------------------------------------

    def _build_reasoning(
        self,
        ticker: str,
        classification: str,
        breakdown: dict[str, int],
        pnl_pct: float,
        exchange: str,
    ) -> str:
        """Build a human-readable reasoning string."""
        parts: list[str] = [
            f"Liquidity: {breakdown['liquidity']}/{self._w_liquidity}",
            f"MarketCap: {breakdown['market_cap']}/{self._w_market_cap}",
            f"Exchange: {breakdown['exchange']}/{self._w_exchange} ({exchange})",
            f"Technical: {breakdown['technical']}/{self._w_technical}",
            f"Sentiment: {breakdown['sentiment']}/{self._w_sentiment}",
            f"Loss: {breakdown['loss']}/{self._w_loss} (P&L {pnl_pct:+.1%})",
        ]

        total: int = sum(breakdown.values())
        summary: str = f"{ticker} total={total} -> {classification}"

        return f"{summary} [{', '.join(parts)}]"

    def _build_recommendation(
        self,
        classification: str,
        ticker: str,
        market_value: float,
        currency: str,
    ) -> str:
        """Build a recommended action string."""
        if classification == "HOLD":
            return f"Place trailing stop at -5% from current price"

        if classification == "SELL":
            return (
                f"Sell {ticker}: limit at mid-price, "
                f"adjust toward bid after {self._adjust_toward_bid_hours}h, "
                f"to bid after {self._adjust_to_bid_hours}h"
            )

        # URGENT_SELL — account is USD-only, market_value is already USD
        value_usd: float = market_value

        if value_usd < self._market_order_threshold:
            return (
                f"URGENT sell {ticker}: start at mid-price, "
                f"use market after {self._adjust_to_bid_hours}h "
                f"(value < ${self._market_order_threshold:.0f})"
            )

        return (
            f"URGENT sell {ticker}: start at mid-price, "
            f"adjust every {self._urgent_adjust_minutes}min toward bid, "
            f"to bid after {self._adjust_toward_bid_hours}h"
        )

    # ------------------------------------------------------------------
    # Execution helpers
    # ------------------------------------------------------------------

    async def _execute_sell(
        self,
        assessment: PositionAssessment,
        gateway: GatewayConnection,
    ) -> dict[str, Any]:
        """Execute a sell order for a SELL or URGENT_SELL position.

        Places a limit sell at the mid-price.  The caller (or a monitoring
        loop) should call :meth:`_adjust_sell_order` periodically.

        Returns an execution result dict.
        """
        ticker: str = assessment.ticker
        exchange: str = assessment.exchange

        bid_ask: tuple[float, float] | None = self._market_data.get_bid_ask(ticker)
        if bid_ask is None:
            logger.warning(
                "%s: No bid/ask data; cannot place sell order", ticker
            )
            return {
                "ticker": ticker,
                "action": "sell",
                "status": "failed",
                "reason": "no_bid_ask_data",
            }

        bid, ask = bid_ask
        mid_price: float = (bid + ask) / 2.0

        logger.info(
            "%s: Placing Phase 0 limit sell at mid=%.4f (bid=%.4f, ask=%.4f)",
            ticker,
            mid_price,
            bid,
            ask,
        )

        try:
            logger.info(
                "%s: Phase 0 sell order prepared (limit=%.4f, classification=%s)",
                ticker,
                mid_price,
                assessment.classification,
            )

            return {
                "ticker": ticker,
                "action": "sell",
                "status": "placed",
                "limit_price": mid_price,
                "classification": assessment.classification,
                "placed_at": datetime.now(ET).isoformat(),
            }

        except Exception:
            logger.exception("%s: Failed to place Phase 0 sell order", ticker)
            return {
                "ticker": ticker,
                "action": "sell",
                "status": "error",
                "reason": "order_placement_failed",
            }

    async def _place_trailing_stop(
        self,
        assessment: PositionAssessment,
        gateway: GatewayConnection,
    ) -> dict[str, Any]:
        """Place a trailing stop for a HOLD position.

        Trailing stop at -5% from current price.
        """
        ticker: str = assessment.ticker

        if assessment.trailing_stop_price is None:
            return {
                "ticker": ticker,
                "action": "trailing_stop",
                "status": "skipped",
                "reason": "no_trailing_stop_price",
            }

        logger.info(
            "%s: Placing Phase 0 trailing stop at %.4f",
            ticker,
            assessment.trailing_stop_price,
        )

        try:
            # The actual IB trailing-stop order would be placed via the
            # order_manager.  Here we record the intent.
            return {
                "ticker": ticker,
                "action": "trailing_stop",
                "status": "placed",
                "stop_price": assessment.trailing_stop_price,
                "placed_at": datetime.now(ET).isoformat(),
            }
        except Exception:
            logger.exception(
                "%s: Failed to place trailing stop", ticker
            )
            return {
                "ticker": ticker,
                "action": "trailing_stop",
                "status": "error",
                "reason": "order_placement_failed",
            }

"""Phase-aware position sizing with settlement awareness.

Calculates position sizes respecting risk limits, settlement constraints,
and phase-specific parameters.  All monetary values are converted to GBP
(the account base currency) for risk calculations, then converted back to
USD for order placement.

Alpaca is commission-free, so no commission checks are needed.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from trading_bot.constants import (
    Market,
)

if TYPE_CHECKING:
    from trading_bot.config import Config
    from trading_bot.data.fx import FXManager
    from trading_bot.execution.settlement_tracker import SettlementTracker

logger: logging.Logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PositionSize:
    """Result of a position-sizing calculation."""

    shares: int
    entry_price: float
    position_value: float       # In USD
    position_value_gbp: float
    risk_amount_gbp: float      # Expected loss if stop hit
    adjustments: list[str]      # Human-readable adjustment descriptions
    is_valid: bool
    rejection_reason: str | None  # Why sizing failed (None when valid)


# ---------------------------------------------------------------------------
# Position sizer
# ---------------------------------------------------------------------------

class PositionSizer:
    """Calculates position sizes respecting all constraints.

    Constraints applied in order (per SPEC Section 6):
      1. Risk-based: max ``risk_per_trade_pct`` of equity at risk
      2. Max position: ``max_position_pct`` of equity
      3. Settlement: do not exceed settled cash
      4. ATR adjustment: reduce 25% if ATR rank > 70
      5. Sentiment adjustment: 75% if no sentiment data
      6. Round down to whole shares
      7. Min position: check against phase minimum
    """

    def __init__(
        self,
        config: Config,
        fx: FXManager,
        settlement: SettlementTracker,
    ) -> None:
        self._config: Config = config
        self._fx: FXManager = fx
        self._settlement: SettlementTracker = settlement

    # ------------------------------------------------------------------
    # Main calculation
    # ------------------------------------------------------------------

    def calculate(
        self,
        ticker: str,
        exchange: str,
        entry_price: float,
        stop_price: float,
        account_equity_gbp: float,
        sentiment_score: float | None,
        atr_rank: float,
    ) -> PositionSize:
        """Calculate position size respecting ALL constraints.

        Parameters
        ----------
        ticker:
            Symbol to trade.
        exchange:
            Exchange string (``'NYSE'``, ``'NASDAQ'``, ``'US'``).
        entry_price:
            Planned entry price in USD.
        stop_price:
            Stop-loss price in USD.
        account_equity_gbp:
            Current account equity in GBP.
        sentiment_score:
            Normalized sentiment score (-1 to 1), or ``None`` if no data.
        atr_rank:
            ATR percentile rank (0-100).

        Returns
        -------
        PositionSize
            Fully populated sizing result.
        """
        adjustments: list[str] = []

        # FX conversion factor: USD -> GBP
        gbp_usd_rate: float = self._fx.get_rate()
        fx_to_gbp: float = 1.0 / gbp_usd_rate if gbp_usd_rate > 0 else 1.0

        # ---- step 1: risk-based sizing ----
        risk_pct: float = self._config.get_risk_per_trade()
        max_risk_gbp: float = account_equity_gbp * risk_pct
        stop_distance: float = abs(entry_price - stop_price)

        if stop_distance <= 0:
            return self._reject(
                entry_price,
                "Stop distance is zero or negative "
                f"(entry={entry_price:.4f}, stop={stop_price:.4f})",
            )

        stop_distance_gbp: float = stop_distance * fx_to_gbp
        shares_from_risk: float = max_risk_gbp / stop_distance_gbp
        shares: int = math.floor(shares_from_risk)
        adjustments.append(
            f"Risk-based: {shares} shares "
            f"(risk={risk_pct:.1%}, max_risk=GBP{max_risk_gbp:.2f}, "
            f"stop_dist={stop_distance:.4f})"
        )

        # ---- step 2: cap by max position pct ----
        max_pos_pct: float = self._config.get_max_position_pct()
        max_pos_gbp: float = account_equity_gbp * max_pos_pct
        max_pos_usd: float = max_pos_gbp / fx_to_gbp if fx_to_gbp > 0 else max_pos_gbp
        max_shares_from_pos: int = math.floor(max_pos_usd / entry_price) if entry_price > 0 else 0

        if shares > max_shares_from_pos:
            adjustments.append(
                f"Max position cap: {shares} -> {max_shares_from_pos} "
                f"(max {max_pos_pct:.0%} of equity)"
            )
            shares = max_shares_from_pos

        # ---- step 3: settlement constraint ----
        pending_gbp: float = self._settlement.get_pending_total_gbp()
        settled_cash_gbp: float = account_equity_gbp - pending_gbp
        if settled_cash_gbp < 0:
            settled_cash_gbp = 0.0

        position_value_usd: float = shares * entry_price
        position_value_gbp: float = position_value_usd * fx_to_gbp
        total_cost_gbp: float = position_value_usd * fx_to_gbp

        if total_cost_gbp > settled_cash_gbp and settled_cash_gbp > 0:
            available_usd: float = (settled_cash_gbp / fx_to_gbp) if fx_to_gbp > 0 else 0.0
            new_shares: int = math.floor(available_usd / entry_price) if entry_price > 0 else 0
            if new_shares < shares:
                adjustments.append(
                    f"Settlement cap: {shares} -> {new_shares} "
                    f"(settled cash GBP{settled_cash_gbp:.2f})"
                )
                shares = new_shares

        # ---- step 4: ATR adjustment ----
        atr_high_pct: float = float(
            self._config._get("strategy", "atr", "high_percentile", default=70)
        )
        atr_reduction: float = float(
            self._config._get("strategy", "atr", "high_vol_size_reduction", default=0.75)
        )
        if atr_rank > atr_high_pct:
            old_shares: int = shares
            shares = math.floor(shares * atr_reduction)
            adjustments.append(
                f"ATR adjustment: {old_shares} -> {shares} "
                f"(ATR rank {atr_rank:.0f} > {atr_high_pct:.0f}, "
                f"x{atr_reduction})"
            )

        # ---- step 5: sentiment adjustment ----
        no_data_mult: float = float(
            self._config._get("entry", "no_data_size_multiplier", default=0.75)
        )
        if sentiment_score is None:
            old_shares = shares
            shares = math.floor(shares * no_data_mult)
            adjustments.append(
                f"No sentiment data: {old_shares} -> {shares} "
                f"(x{no_data_mult})"
            )

        # ---- step 6: round to whole shares (already int, but enforce) ----
        shares = max(shares, 0)

        # ---- recalculate final values ----
        position_value_usd = shares * entry_price
        position_value_gbp = position_value_usd * fx_to_gbp
        risk_amount_gbp: float = shares * stop_distance_gbp

        # ---- step 7: minimum position value ----
        min_value: float = self._config.get_min_position_value(Market.US)
        if position_value_usd < min_value and shares > 0:
            return self._reject(
                entry_price,
                f"Position value USD{position_value_usd:.2f} "
                f"below minimum USD{min_value:.2f}",
            )

        if shares <= 0:
            return self._reject(entry_price, "Calculated 0 shares after all constraints")

        return PositionSize(
            shares=shares,
            entry_price=entry_price,
            position_value=position_value_usd,
            position_value_gbp=position_value_gbp,
            risk_amount_gbp=risk_amount_gbp,
            adjustments=adjustments,
            is_valid=True,
            rejection_reason=None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _reject(entry_price: float, reason: str) -> PositionSize:
        """Build a rejected PositionSize result."""
        logger.info("Position sizing rejected: %s", reason)
        return PositionSize(
            shares=0,
            entry_price=entry_price,
            position_value=0.0,
            position_value_gbp=0.0,
            risk_amount_gbp=0.0,
            adjustments=[],
            is_valid=False,
            rejection_reason=reason,
        )

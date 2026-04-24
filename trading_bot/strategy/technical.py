"""Technical indicator calculations for the trading strategy.

Computes EMA crossovers, Bollinger Band bounces, volume confirmation,
and ATR percentile ranking from OHLCV bar data using pure pandas.
All methods are stateless: they accept a DataFrame and return a result.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from trading_bot.config import Config

logger: logging.Logger = logging.getLogger(__name__)


class TechnicalAnalyzer:
    """Computes technical indicators from OHLCV bar data.

    Reads all tunables from config.yaml under ``strategy.*``.
    Uses 5-minute bars for EMA, Bollinger, and volume signals.
    Uses daily bars for ATR percentile ranking.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self, config: Config) -> None:
        # EMA
        self._ema_fast: int = int(config._require("strategy", "ema", "fast_period"))
        self._ema_slow: int = int(config._require("strategy", "ema", "slow_period"))
        self._ema_crossover_lookback: int = int(
            config._require("strategy", "ema", "crossover_lookback_bars")
        )

        # Bollinger Bands
        self._bb_period: int = int(config._require("strategy", "bollinger", "period"))
        self._bb_std: float = float(config._require("strategy", "bollinger", "std_dev"))
        self._bb_bounce_lookback: int = int(
            config._require("strategy", "bollinger", "bounce_lookback_bars")
        )
        self._bb_squeeze_threshold: float = float(
            config._require("strategy", "bollinger", "squeeze_threshold")
        )

        # Volume
        self._vol_avg_period: int = int(
            config._require("strategy", "volume", "average_period")
        )
        self._vol_multiplier: float = float(
            config._require("strategy", "volume", "multiplier")
        )

        # ATR
        self._atr_period: int = int(config._require("strategy", "atr", "period"))
        self._atr_lookback_days: int = int(
            config._require("strategy", "atr", "rank_lookback_days")
        )
        self._atr_extreme: float = float(
            config._require("strategy", "atr", "extreme_percentile")
        )
        self._atr_high: float = float(
            config._require("strategy", "atr", "high_percentile")
        )

    # ------------------------------------------------------------------
    # DataFrame enrichment
    # ------------------------------------------------------------------

    def compute_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add all technical indicators to a DataFrame with OHLCV columns.

        Expected columns: ``open``, ``high``, ``low``, ``close``, ``volume``
        (case-insensitive -- will be lowered automatically).

        Returns *df* with added columns:
        ``ema_fast``, ``ema_slow``, ``bb_upper``, ``bb_mid``, ``bb_lower``,
        ``bb_bandwidth``, ``vol_avg``.
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]

        if len(df) < self._ema_slow + 5:
            logger.warning(
                "DataFrame has only %d rows; need at least %d for reliable EMA",
                len(df),
                self._ema_slow + 5,
            )

        # EMA — pure pandas ewm
        df["ema_fast"] = df["close"].ewm(span=self._ema_fast, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=self._ema_slow, adjust=False).mean()

        # Bollinger Bands — SMA ± std_dev * rolling std
        df["bb_mid"] = df["close"].rolling(window=self._bb_period).mean()
        rolling_std: pd.Series = df["close"].rolling(window=self._bb_period).std()
        df["bb_upper"] = df["bb_mid"] + (self._bb_std * rolling_std)
        df["bb_lower"] = df["bb_mid"] - (self._bb_std * rolling_std)
        df["bb_bandwidth"] = np.where(
            df["bb_mid"] > 0,
            (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"],
            float("nan"),
        )

        # Volume average
        df["vol_avg"] = df["volume"].rolling(window=self._vol_avg_period).mean()

        return df

    def compute_atr(self, df: pd.DataFrame) -> pd.DataFrame:
        """Compute ATR on a DataFrame (typically daily bars).

        Adds an ``atr`` column using the configured ATR period.
        Uses Wilder's smoothing (exponential moving average of True Range).
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]

        # True Range = max(H-L, |H-prevC|, |L-prevC|)
        prev_close: pd.Series = df["close"].shift(1)
        tr1: pd.Series = df["high"] - df["low"]
        tr2: pd.Series = (df["high"] - prev_close).abs()
        tr3: pd.Series = (df["low"] - prev_close).abs()
        true_range: pd.Series = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

        # Wilder's smoothing (equivalent to EMA with alpha=1/period)
        df["atr"] = true_range.ewm(alpha=1.0 / self._atr_period, adjust=False).mean()
        return df

    # ------------------------------------------------------------------
    # Signal checks
    # ------------------------------------------------------------------

    def check_ema_crossover(
        self, df: pd.DataFrame, lookback: int | None = None
    ) -> bool:
        """Check if fast EMA crossed above slow EMA within *lookback* bars.

        A crossover is detected when ``ema_fast`` was at or below ``ema_slow``
        at some bar within the lookback window, and is now strictly above it.
        """
        lb: int = lookback if lookback is not None else self._ema_crossover_lookback
        if len(df) < lb + 1:
            return False

        tail: pd.DataFrame = df.iloc[-(lb + 1) :]
        fast: pd.Series = tail["ema_fast"]
        slow: pd.Series = tail["ema_slow"]

        if fast.isna().any() or slow.isna().any():
            return False

        # Current bar: fast must be above slow
        if fast.iloc[-1] <= slow.iloc[-1]:
            return False

        # At some earlier bar in the window, fast was at or below slow
        for i in range(len(tail) - 1):
            if fast.iloc[i] <= slow.iloc[i]:
                return True

        return False

    def check_ema_crossunder(
        self, df: pd.DataFrame, lookback: int | None = None
    ) -> bool:
        """Check if fast EMA crossed below slow EMA within *lookback* bars.

        Mirror of :meth:`check_ema_crossover`.
        """
        lb: int = lookback if lookback is not None else self._ema_crossover_lookback
        if len(df) < lb + 1:
            return False

        tail: pd.DataFrame = df.iloc[-(lb + 1) :]
        fast: pd.Series = tail["ema_fast"]
        slow: pd.Series = tail["ema_slow"]

        if fast.isna().any() or slow.isna().any():
            return False

        # Current bar: fast must be below slow
        if fast.iloc[-1] >= slow.iloc[-1]:
            return False

        # At some earlier bar, fast was at or above slow
        for i in range(len(tail) - 1):
            if fast.iloc[i] >= slow.iloc[i]:
                return True

        return False

    def check_bollinger_bounce(
        self, df: pd.DataFrame, lookback: int | None = None
    ) -> str | None:
        """Check for a Bollinger Band bounce.

        Returns ``'long'`` if price touched/penetrated the lower band within
        the lookback window and has since reversed upward (current close >
        previous close).

        Returns ``'short'`` if price touched the upper band and reversed
        downward.

        Returns ``None`` if no bounce detected.
        """
        lb: int = lookback if lookback is not None else self._bb_bounce_lookback
        if len(df) < lb + 1:
            return None

        tail: pd.DataFrame = df.iloc[-(lb + 1) :]

        for col in ("bb_lower", "bb_upper", "close", "low", "high"):
            if col not in tail.columns or tail[col].isna().all():
                return None

        current_close: float = float(tail["close"].iloc[-1])
        prev_close: float = float(tail["close"].iloc[-2])

        # Check lower band touch -> long bounce
        for i in range(len(tail) - 1):  # exclude current bar from touch check
            low_val: float = float(tail["low"].iloc[i])
            bb_lower_val: float = float(tail["bb_lower"].iloc[i])
            if not (math.isnan(low_val) or math.isnan(bb_lower_val)):
                if low_val <= bb_lower_val:
                    if current_close > prev_close:
                        return "long"

        # Check upper band touch -> short bounce
        for i in range(len(tail) - 1):
            high_val: float = float(tail["high"].iloc[i])
            bb_upper_val: float = float(tail["bb_upper"].iloc[i])
            if not (math.isnan(high_val) or math.isnan(bb_upper_val)):
                if high_val >= bb_upper_val:
                    if current_close < prev_close:
                        return "short"

        return None

    def check_volume_confirmation(self, df: pd.DataFrame) -> bool:
        """Check if current bar volume exceeds the threshold.

        Returns ``True`` when current volume > multiplier * vol_avg.
        """
        if len(df) < 1:
            return False

        if "volume" not in df.columns or "vol_avg" not in df.columns:
            return False

        current_vol: float = float(df["volume"].iloc[-1])
        vol_avg: float = float(df["vol_avg"].iloc[-1])

        if math.isnan(current_vol) or math.isnan(vol_avg) or vol_avg <= 0:
            return False

        return current_vol > (self._vol_multiplier * vol_avg)

    def check_squeeze(self, df: pd.DataFrame) -> bool:
        """Check if Bollinger Bands are in a squeeze (low bandwidth).

        Returns ``True`` when ``bb_bandwidth < squeeze_threshold``.
        """
        if len(df) < 1 or "bb_bandwidth" not in df.columns:
            return False

        bw: float = float(df["bb_bandwidth"].iloc[-1])
        if math.isnan(bw):
            return False

        return bw < self._bb_squeeze_threshold

    # ------------------------------------------------------------------
    # ATR percentile ranking
    # ------------------------------------------------------------------

    def get_atr_percentile_rank(self, daily_df: pd.DataFrame) -> float:
        """Compute ATR percentile rank (0-100) against historical daily bars.

        Uses the configured ATR period on *daily_df* and ranks today's ATR
        value against the last ``atr_lookback_days`` days of ATR values.

        Returns 0.0 if insufficient data.
        """
        if len(daily_df) < self._atr_period + 1:
            logger.debug(
                "Insufficient daily bars (%d) for ATR rank; need at least %d",
                len(daily_df),
                self._atr_period + 1,
            )
            return 0.0

        enriched: pd.DataFrame = self.compute_atr(daily_df)
        atr_series: pd.Series = enriched["atr"].dropna()

        if len(atr_series) < 2:
            return 0.0

        # Take the last N values for the lookback window
        lookback: pd.Series = atr_series.iloc[-self._atr_lookback_days :]
        current_atr: float = float(lookback.iloc[-1])

        if math.isnan(current_atr):
            return 0.0

        # Percentile rank: fraction of historical values <= current
        count_below: int = int((lookback < current_atr).sum())
        rank: float = (count_below / len(lookback)) * 100.0

        logger.debug(
            "ATR rank: %.1f (current ATR=%.4f, lookback=%d bars)",
            rank,
            current_atr,
            len(lookback),
        )
        return rank

    # ------------------------------------------------------------------
    # General-purpose indicator helpers
    # ------------------------------------------------------------------

    @staticmethod
    def compute_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Compute RSI on close prices. Returns a Series aligned to *df*."""
        close: pd.Series = df["close"] if "close" in df.columns else df["Close"]
        delta: pd.Series = close.diff()
        gain: pd.Series = delta.clip(lower=0.0)
        loss: pd.Series = (-delta).clip(lower=0.0)
        avg_gain: pd.Series = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        avg_loss: pd.Series = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
        rs: pd.Series = avg_gain / avg_loss.replace(0, float("nan"))
        rsi: pd.Series = 100.0 - (100.0 / (1.0 + rs))
        return rsi

    @staticmethod
    def compute_sma(df: pd.DataFrame, period: int) -> pd.Series:
        """Compute simple moving average on close prices."""
        close: pd.Series = df["close"] if "close" in df.columns else df["Close"]
        return close.rolling(window=period).mean()

    @staticmethod
    def get_period_high(df: pd.DataFrame, period: int) -> float:
        """Return the highest high over the last *period* bars."""
        col: str = "high" if "high" in df.columns else "High"
        if len(df) < period:
            return float(df[col].max())
        return float(df[col].iloc[-period:].max())

    @staticmethod
    def get_period_low(df: pd.DataFrame, period: int) -> float:
        """Return the lowest low over the last *period* bars."""
        col: str = "low" if "low" in df.columns else "Low"
        if len(df) < period:
            return float(df[col].min())
        return float(df[col].iloc[-period:].min())

    # ------------------------------------------------------------------
    # Composite signal evaluation
    # ------------------------------------------------------------------

    def get_signals(
        self, df_5min: pd.DataFrame, df_daily: pd.DataFrame
    ) -> dict[str, Any]:
        """Evaluate all technical signals.

        Args:
            df_5min: 5-minute OHLCV bars (will be enriched if needed).
            df_daily: Daily OHLCV bars for ATR ranking.

        Returns a dict with:
            ``ema_cross``       : bool -- whether an EMA cross occurred
            ``ema_direction``   : 'long' | 'short' | None
            ``bb_bounce``       : 'long' | 'short' | None
            ``volume_confirmed``: bool
            ``atr_rank``        : float (0-100)
            ``squeeze``         : bool -- BB squeeze (informational)
            ``signal_count``    : int -- how many of the 3 core signals fire
            ``direction``       : 'long' | 'short' | None -- consensus
        """
        # Ensure indicators are computed
        if "ema_fast" not in df_5min.columns:
            df_5min = self.compute_indicators(df_5min)

        # EMA cross
        ema_cross_long: bool = self.check_ema_crossover(df_5min)
        ema_cross_short: bool = self.check_ema_crossunder(df_5min)
        ema_cross: bool = ema_cross_long or ema_cross_short
        ema_direction: str | None = None
        if ema_cross_long:
            ema_direction = "long"
        elif ema_cross_short:
            ema_direction = "short"

        # Bollinger bounce
        bb_bounce: str | None = self.check_bollinger_bounce(df_5min)

        # Volume
        volume_confirmed: bool = self.check_volume_confirmation(df_5min)

        # ATR rank (from daily bars)
        atr_rank: float = self.get_atr_percentile_rank(df_daily)

        # Squeeze (informational, not a trade signal in Phase 1)
        squeeze: bool = self.check_squeeze(df_5min)
        if squeeze:
            logger.debug("Bollinger squeeze detected -- potential breakout imminent")

        # Count aligned signals
        signal_count: int = 0
        direction_votes: dict[str, int] = {"long": 0, "short": 0}

        if ema_cross:
            signal_count += 1
            if ema_direction is not None:
                direction_votes[ema_direction] += 1

        if bb_bounce is not None:
            signal_count += 1
            direction_votes[bb_bounce] += 1

        if volume_confirmed:
            signal_count += 1

        # Overall direction -- directional signals must agree, no conflict
        direction: str | None = None
        if direction_votes["long"] > 0 and direction_votes["short"] == 0:
            direction = "long"
        elif direction_votes["short"] > 0 and direction_votes["long"] == 0:
            direction = "short"
        # Conflicting directions (long EMA + short BB) -> None

        return {
            "ema_cross": ema_cross,
            "ema_direction": ema_direction,
            "bb_bounce": bb_bounce,
            "volume_confirmed": volume_confirmed,
            "atr_rank": atr_rank,
            "squeeze": squeeze,
            "signal_count": signal_count,
            "direction": direction,
        }

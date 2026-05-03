"""Property-based tests for position sizing.

Manual review caught the ``PositionSize.shares: int`` truncation trap
([ai-broker#55]). Property tests catch the same shape of bug — a quiet
zero or wrong sign — automatically and across thousands of inputs.

The defects in this repo are mostly silent: wrong types that floor to
zero, naive datetimes that drift by a day, and sync calls that stall a
tick. Property tests turn "silent wrong" into "loud failure" for the
truncation surface.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from trading_bot.strategy.base import StrategyBase

pytestmark = pytest.mark.critical


# ---------------------------------------------------------------------
# Reasonable-input strategies
# ---------------------------------------------------------------------
# Bound floats away from extremes so we exercise the realistic operating
# envelope, not floating-point edge cases. The contract we're protecting
# is "given valid trading inputs, never silently truncate to 0".

valid_price = st.floats(
    min_value=1.0, max_value=10_000.0,
    allow_nan=False, allow_infinity=False,
)
valid_cash = st.floats(
    min_value=100.0, max_value=10_000_000.0,
    allow_nan=False, allow_infinity=False,
)
valid_risk_pct = st.floats(
    min_value=0.001, max_value=0.10,
    allow_nan=False, allow_infinity=False,
)
valid_max_pos_pct = st.floats(
    min_value=0.05, max_value=1.0,
    allow_nan=False, allow_infinity=False,
)
valid_stop_pct = st.floats(
    min_value=0.001, max_value=0.20,
    allow_nan=False, allow_infinity=False,
)
valid_vol_mult = st.floats(
    min_value=0.1, max_value=3.0,
    allow_nan=False, allow_infinity=False,
)


# ---------------------------------------------------------------------
# Output invariants
# ---------------------------------------------------------------------

@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=valid_price,
    cash=valid_cash,
    risk_pct=valid_risk_pct,
    max_pos_pct=valid_max_pos_pct,
    stop_pct=valid_stop_pct,
    vol_mult=valid_vol_mult,
)
def test_size_by_risk_never_negative(
    entry: float, cash: float, risk_pct: float,
    max_pos_pct: float, stop_pct: float, vol_mult: float,
) -> None:
    """``size_by_risk`` returns a non-negative count for any valid input."""
    stop = entry * (1.0 - stop_pct)
    shares = StrategyBase.size_by_risk(
        entry_price=entry,
        stop_price=stop,
        available_cash=cash,
        risk_per_trade_pct=risk_pct,
        max_position_pct=max_pos_pct,
        fractional=True,
        vol_multiplier=vol_mult,
    )
    assert shares >= 0.0, f"negative shares={shares}"


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=valid_price,
    cash=valid_cash,
    risk_pct=valid_risk_pct,
    max_pos_pct=valid_max_pos_pct,
    stop_pct=valid_stop_pct,
    vol_mult=valid_vol_mult,
)
def test_size_by_risk_respects_max_position_cap(
    entry: float, cash: float, risk_pct: float,
    max_pos_pct: float, stop_pct: float, vol_mult: float,
) -> None:
    """Position value never exceeds ``max_position_pct * available_cash``.

    Floating-point rounding in ``round(shares, 4)`` can push the
    notional just above the cap by at most one share-step (1e-4 *
    entry). Allow that as the only slack.
    """
    stop = entry * (1.0 - stop_pct)
    shares = StrategyBase.size_by_risk(
        entry_price=entry,
        stop_price=stop,
        available_cash=cash,
        risk_per_trade_pct=risk_pct,
        max_position_pct=max_pos_pct,
        fractional=True,
        vol_multiplier=vol_mult,
    )
    notional = shares * entry
    cap = cash * max_pos_pct
    rounding_slack = 1e-4 * entry  # one fractional-share step at this price
    assert notional <= cap + rounding_slack, (
        f"notional={notional:.4f} > cap={cap:.4f} (slack={rounding_slack:.6f})"
    )


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=valid_price,
    cash=valid_cash,
    risk_pct=valid_risk_pct,
    max_pos_pct=valid_max_pos_pct,
    stop_pct=valid_stop_pct,
)
def test_fractional_size_does_not_truncate_to_zero(
    entry: float, cash: float, risk_pct: float,
    max_pos_pct: float, stop_pct: float,
) -> None:
    """The truncation trap from [ai-broker#55].

    With ``fractional=True``, the only legitimate reasons to return
    exactly 0 are: (a) the cash budget at this price gives < 1e-4 shares
    (the rounding floor of ``round(.., 4)``), or (b) the risk budget is
    smaller than the rounding floor.

    For any input where both budgets give >= 1e-4 shares, sizing must
    NOT silently return 0. This is the property the future "shares: int"
    refactor would have failed.
    """
    stop = entry * (1.0 - stop_pct)
    risk_per_share = entry - stop
    cash_budget_shares = (cash * max_pos_pct) / entry
    risk_budget_shares = (cash * risk_pct) / risk_per_share

    shares = StrategyBase.size_by_risk(
        entry_price=entry,
        stop_price=stop,
        available_cash=cash,
        risk_per_trade_pct=risk_pct,
        max_position_pct=max_pos_pct,
        fractional=True,
    )

    # If both budgets clearly support a non-trivial size (>= 0.001 shares),
    # the result must be > 0. 0.001 leaves comfortable headroom above the
    # 0.0001 round() floor for floating-point error.
    if cash_budget_shares >= 0.001 and risk_budget_shares >= 0.001:
        assert shares > 0.0, (
            f"silent zero — cash_budget={cash_budget_shares:.6f}, "
            f"risk_budget={risk_budget_shares:.6f}, "
            f"entry={entry}, stop={stop}, cash={cash}"
        )


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=valid_price,
    cash=valid_cash,
    risk_pct=valid_risk_pct,
    max_pos_pct=valid_max_pos_pct,
    stop_pct=valid_stop_pct,
)
def test_whole_share_size_is_int_valued(
    entry: float, cash: float, risk_pct: float,
    max_pos_pct: float, stop_pct: float,
) -> None:
    """With ``fractional=False`` the result is always integer-valued."""
    stop = entry * (1.0 - stop_pct)
    shares = StrategyBase.size_by_risk(
        entry_price=entry,
        stop_price=stop,
        available_cash=cash,
        risk_per_trade_pct=risk_pct,
        max_position_pct=max_pos_pct,
        fractional=False,
    )
    assert shares == int(shares), f"non-integer whole-share count: {shares}"


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
@given(
    entry=valid_price,
    cash=valid_cash,
    risk_pct=valid_risk_pct,
    max_pos_pct=valid_max_pos_pct,
    stop_pct=valid_stop_pct,
)
def test_zero_or_negative_inputs_return_zero(
    entry: float, cash: float, risk_pct: float,
    max_pos_pct: float, stop_pct: float,
) -> None:
    """Pathological entry/cash/stop produce a clean 0, never a negative."""
    stop = entry * (1.0 - stop_pct)
    # Drive each pathological branch.
    assert StrategyBase.size_by_risk(
        entry_price=0.0, stop_price=stop, available_cash=cash,
        risk_per_trade_pct=risk_pct, max_position_pct=max_pos_pct,
        fractional=True,
    ) == 0.0
    assert StrategyBase.size_by_risk(
        entry_price=entry, stop_price=stop, available_cash=0.0,
        risk_per_trade_pct=risk_pct, max_position_pct=max_pos_pct,
        fractional=True,
    ) == 0.0
    # stop == entry → risk_per_share == 0 → 0 shares
    assert StrategyBase.size_by_risk(
        entry_price=entry, stop_price=entry, available_cash=cash,
        risk_per_trade_pct=risk_pct, max_position_pct=max_pos_pct,
        fractional=True,
    ) == 0.0

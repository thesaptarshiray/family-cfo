"""
XIRR: a pure calculation, no persistence, no I/O.

The extended internal rate of return for a series of dated, irregularly
spaced cash flows — the standard way to express a return that accounts for
*when* money moved, not just how much. There is no closed-form solution; the
rate is the root of

    f(rate) = sum_i cash_flow_i / (1 + rate) ** (days_i / 365)

where `days_i` is the number of days from the first cash flow to cash flow
`i`. This module finds that root by bisection rather than Newton's method:
bisection needs no derivative, and it either converges to a solution
bracketed between two rates of opposite-signed `f`, or it reports that none
was found — it cannot diverge or oscillate the way Newton's method can on an
oddly shaped cash-flow series.

Decimal throughout, including the fractional exponent above: Python's
`decimal.Decimal` supports `**` with a non-integer exponent natively
(computed via the decimal module's own correctly-rounded algorithms, never
routed through a binary `float`), so this satisfies the Financial Engine's
"no float" rule while still doing real exponentiation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List, Optional, Sequence, Tuple

_DAYS_PER_YEAR = Decimal("365")
_LOW_RATE_BOUND = Decimal("-0.9999")  # -100% is undefined (division by zero)
_HIGH_RATE_BOUND = Decimal("100")  # 10,000% annualised: a generous ceiling
_TOLERANCE = Decimal("0.00001")
_MAX_ITERATIONS = 200


def _net_present_value(cash_flows: Sequence[Tuple[int, Decimal]], rate: Decimal) -> Decimal:
    total = Decimal("0")
    base = Decimal(1) + rate
    for days, amount in cash_flows:
        total += amount / (base ** (Decimal(days) / _DAYS_PER_YEAR))
    return total


def compute_xirr(cash_flows: Sequence[Tuple[date, Decimal]]) -> Optional[Decimal]:
    """Return the annualised XIRR for `cash_flows`, or `None` if none exists.

    Each entry is `(date, amount)`; money going out (a purchase) is negative,
    money coming in or the current value of what you still hold (as if sold
    today) is positive — the caller decides the sign, this function just
    solves the equation. Returns `None` when there are fewer than two flows,
    when every flow has the same sign (no rate makes such a series net to
    zero), or when no root was found within the search bounds — never raises
    for those cases, since "cannot be computed yet" is a normal state for a
    holding with only one transaction so far.
    """
    if len(cash_flows) < 2:
        return None

    ordered = sorted(cash_flows, key=lambda cf: cf[0])
    first_date = ordered[0][0]
    relative = [((d - first_date).days, amount) for d, amount in ordered]

    has_negative = any(amount < 0 for _, amount in relative)
    has_positive = any(amount > 0 for _, amount in relative)
    if not (has_negative and has_positive):
        return None

    low, high = _LOW_RATE_BOUND, _HIGH_RATE_BOUND
    try:
        f_low = _net_present_value(relative, low)
        f_high = _net_present_value(relative, high)
    except (InvalidOperation, OverflowError, ZeroDivisionError):
        return None

    if f_low == 0:
        return low.quantize(_TOLERANCE)
    if f_high == 0:
        return high.quantize(_TOLERANCE)
    if (f_low > 0) == (f_high > 0):
        return None  # same sign at both bounds: no root bracketed here

    for _ in range(_MAX_ITERATIONS):
        mid = (low + high) / 2
        try:
            f_mid = _net_present_value(relative, mid)
        except (InvalidOperation, OverflowError, ZeroDivisionError):
            return None

        if abs(f_mid) < Decimal("1e-6") or (high - low) < _TOLERANCE:
            return mid.quantize(_TOLERANCE)

        if (f_mid > 0) == (f_low > 0):
            low, f_low = mid, f_mid
        else:
            high = mid

    return None  # did not converge within _MAX_ITERATIONS

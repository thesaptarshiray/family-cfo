"""
Fixed-deposit valuation: a pure calculation, no persistence, no I/O.

FD is the deterministic asset class (see the companion product assessment):
its value is a function of terms already known at opening, not a market price
that has to be looked up. This module is that function.

The formula is deliberately simple for v1, and depends on `payout_type`:

  - `QUARTERLY` / `MONTHLY` / `MANUAL` (interest paid out as cash, not reinvested in the
    FD): the FD's own book value does not grow between issue and maturity —
    the interest is not compounding inside it. Current value is the
    principal, flat, for the FD's whole life. `MANUAL` additionally signals
    "no fixed schedule to compute from" — a future manual revaluation action
    (distinct from this formula) is the right tool for it, not this one.

  - `MATURITY` (cumulative — interest compounds into the FD): value is
    interpolated linearly between `principal` (at `issue_date`) and a target
    value at `maturity_date`. The target is `maturity_value` when the caller
    has it (this is exactly the bank-quoted number, e.g. from a paper
    statement or Excel column — using it directly is more honest than
    guessing the bank's compounding convention). When `maturity_value` is not
    known, it is projected with quarterly compounding — how Indian banks
    compute cumulative FDs — as an approximation. Conversely, when only the
    maturity value is known, `derive_fd_interest_rate` recovers the rate on
    the same convention, so either one is enough to enter a cumulative FD.

This is intentionally not quarterly-compounded bank-exact interest — see the
product assessment for why a v1 keeps this simple. It never uses `float`
(the Financial Engine architecture rule) and never touches the database.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

CUMULATIVE_PAYOUT_TYPE = "MATURITY"
_CENTS = Decimal("0.01")
_DAYS_PER_YEAR = Decimal("365")


_QUARTERS_PER_YEAR = Decimal("4")


def _projected_maturity_value(
    principal: Decimal, interest_rate: Decimal, total_days: int
) -> Decimal:
    """Quarterly-compounded projection, used only when no bank-quoted
    maturity value is available: principal x (1 + rate/400) ^ (4 x years).
    `interest_rate` is an annual percentage."""
    years = Decimal(total_days) / _DAYS_PER_YEAR
    growth = (Decimal("1") + interest_rate / Decimal("100") / _QUARTERS_PER_YEAR) ** (
        _QUARTERS_PER_YEAR * years
    )
    return principal * growth


def derive_fd_interest_rate(
    *, principal: Decimal, maturity_value: Decimal, issue_date: date, maturity_date: date
) -> Decimal:
    """The annual rate (%, 2 decimals) that grows `principal` to
    `maturity_value` by `maturity_date` with quarterly compounding — the
    inverse of the projection above, for a cumulative FD whose rate the user
    does not remember."""
    total_days = (maturity_date - issue_date).days
    if total_days <= 0:
        raise ValueError("maturity date must be after the issue date")
    if maturity_value <= principal:
        raise ValueError("maturity amount must be more than the principal to work out the rate")
    quarters = _QUARTERS_PER_YEAR * Decimal(total_days) / _DAYS_PER_YEAR
    per_quarter = (maturity_value / principal) ** (Decimal("1") / quarters) - Decimal("1")
    return (per_quarter * _QUARTERS_PER_YEAR * Decimal("100")).quantize(_CENTS, rounding=ROUND_HALF_UP)


def compute_fd_current_value(
    *,
    principal: Decimal,
    interest_rate: Decimal,
    issue_date: date,
    maturity_date: date,
    payout_type: str,
    maturity_value: Optional[Decimal],
    as_of: date,
) -> Decimal:
    """Return the FD's projected value as of `as_of`, rounded to the cent.

    Before `issue_date`, or for a non-cumulative `payout_type`, this is just
    `principal`. At or after `maturity_date`, it is the known or projected
    maturity value. In between, for a `MATURITY`-type FD, it is a straight
    line from `principal` to that maturity value.
    """
    if as_of <= issue_date or payout_type != CUMULATIVE_PAYOUT_TYPE:
        return principal.quantize(_CENTS, rounding=ROUND_HALF_UP)

    total_days = (maturity_date - issue_date).days
    if total_days <= 0:
        # Guards against a malformed term; the schema's own CHECK
        # (maturity_date > issue_date) should make this unreachable.
        return principal.quantize(_CENTS, rounding=ROUND_HALF_UP)

    target = (
        maturity_value
        if maturity_value is not None
        else _projected_maturity_value(principal, interest_rate, total_days)
    )

    if as_of >= maturity_date:
        value = target
    else:
        elapsed_days = (as_of - issue_date).days
        value = principal + (target - principal) * Decimal(elapsed_days) / Decimal(total_days)

    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


# Interest payouts per year, for FDs that pay interest out rather than
# accumulating it. MATURITY (cumulative) and MANUAL have no fixed schedule.
PAYOUTS_PER_YEAR = {"QUARTERLY": 4, "MONTHLY": 12}


def compute_fd_payout_interest(
    *, principal: Decimal, interest_rate: Decimal, payout_type: str
) -> Optional[Decimal]:
    """Interest paid out each period (quarter or month), rounded to the
    rupee-paisa: principal x annual rate / payouts per year. `None` for FDs
    without a regular payout.

    An approximation: banks compute each payout on the actual days in the
    period (and some discount monthly payouts slightly), so a statement can
    differ by a few rupees.
    """
    per_year = PAYOUTS_PER_YEAR.get(payout_type)
    if per_year is None:
        return None
    return (principal * interest_rate / Decimal("100") / Decimal(per_year)).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )


def compute_fd_interest(
    *,
    principal: Decimal,
    interest_rate: Decimal,
    issue_date: date,
    maturity_date: date,
    payout_type: str,
    maturity_value: Optional[Decimal],
    as_of: date,
) -> "tuple[Decimal, Decimal]":
    """(interest earned so far, total interest by maturity) for one FD.

    Cumulative (MATURITY) FDs: the growth in the FD's own value — current
    value minus principal, and maturity value minus principal.

    Payout FDs (QUARTERLY / MONTHLY / MANUAL): the interest is paid out, so
    the FD's value stays at principal and this is the only place that
    interest shows up. Simple interest on the principal by days —
    principal x rate x days / 365 — from the issue date to today (capped at
    maturity) and over the whole term. Counting days rather than whole
    quarters covers a broken final period; for a term of exact quarters it
    equals payout x number of payouts.
    """
    if payout_type == CUMULATIVE_PAYOUT_TYPE:
        def value(on: date) -> Decimal:
            return compute_fd_current_value(
                principal=principal, interest_rate=interest_rate, issue_date=issue_date,
                maturity_date=maturity_date, payout_type=payout_type,
                maturity_value=maturity_value, as_of=on,
            )
        return value(as_of) - principal, value(maturity_date) - principal

    def simple(days: int) -> Decimal:
        return (
            principal * interest_rate / Decimal("100") * Decimal(max(days, 0)) / _DAYS_PER_YEAR
        ).quantize(_CENTS, rounding=ROUND_HALF_UP)

    earned_until = min(as_of, maturity_date)
    return simple((earned_until - issue_date).days), simple((maturity_date - issue_date).days)


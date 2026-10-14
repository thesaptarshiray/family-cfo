"""Tests for `financial_engine.fd_valuation.compute_fd_current_value`.

Pure function, no database — every case is exercised directly.
"""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine.fd_valuation import compute_fd_current_value

ISSUE = date(2025, 1, 1)
MATURITY = date(2026, 1, 1)  # exactly 365 days


def value(**overrides):
    fields = dict(
        principal=Decimal("100000"),
        interest_rate=Decimal("7.00"),
        issue_date=ISSUE,
        maturity_date=MATURITY,
        payout_type="MATURITY",
        maturity_value=None,
        as_of=ISSUE,
    )
    fields.update(overrides)
    return compute_fd_current_value(**fields)


# ----------------------------------------------------------------------
# Non-cumulative payout types: flat at principal, no compounding assumed
# ----------------------------------------------------------------------


@pytest.mark.parametrize("payout_type", ["QUARTERLY", "MANUAL"])
def test_non_cumulative_payout_is_flat_at_principal_throughout_the_term(payout_type):
    for as_of in (ISSUE, date(2025, 7, 1), MATURITY, date(2026, 6, 1)):
        assert value(payout_type=payout_type, as_of=as_of) == Decimal("100000.00")


def test_non_cumulative_payout_ignores_a_supplied_maturity_value():
    assert value(
        payout_type="QUARTERLY", maturity_value=Decimal("107000"), as_of=MATURITY
    ) == Decimal("100000.00")


# ----------------------------------------------------------------------
# Before issue / at issue: always principal
# ----------------------------------------------------------------------


def test_before_or_at_issue_date_is_principal():
    assert value(as_of=date(2024, 6, 1)) == Decimal("100000.00")
    assert value(as_of=ISSUE) == Decimal("100000.00")


# ----------------------------------------------------------------------
# MATURITY payout, with a bank-quoted maturity_value: linear interpolation
# ----------------------------------------------------------------------


def test_maturity_type_interpolates_linearly_to_a_known_maturity_value():
    # 100000 -> 107000 over 365 days; halfway (day ~182) should be ~half the growth.
    halfway = date(2025, 7, 2)  # 182 days elapsed of 365
    result = value(as_of=halfway, maturity_value=Decimal("107000"))
    expected = Decimal("100000") + Decimal("7000") * Decimal(182) / Decimal(365)
    assert result == expected.quantize(Decimal("0.01"))
    assert Decimal("103400") < result < Decimal("103500")  # sanity bound


def test_maturity_type_reaches_the_known_maturity_value_at_and_after_maturity():
    assert value(as_of=MATURITY, maturity_value=Decimal("107000")) == Decimal("107000.00")
    assert value(as_of=date(2027, 1, 1), maturity_value=Decimal("107000")) == Decimal("107000.00")


def test_maturity_type_never_exceeds_or_undershoots_its_endpoints():
    quoted = Decimal("107000")
    prev = Decimal("100000")
    d = ISSUE
    while d <= MATURITY:
        v = value(as_of=d, maturity_value=quoted)
        assert Decimal("100000.00") <= v <= Decimal("107000.00")
        assert v >= prev
        prev = v
        d = date.fromordinal(d.toordinal() + 45)


# ----------------------------------------------------------------------
# MATURITY payout, no maturity_value given: quarterly-compounded projection
# ----------------------------------------------------------------------


def test_maturity_type_without_a_quoted_value_projects_quarterly_compounding():
    # 100000 at 7% for exactly one year, compounded quarterly: 100000 x 1.0175^4
    assert value(as_of=MATURITY, maturity_value=None) == Decimal("107185.90")


def test_maturity_type_without_a_quoted_value_interpolates_toward_the_projection():
    halfway = date(2025, 7, 2)
    result = value(as_of=halfway, maturity_value=None)
    assert Decimal("103500") < result < Decimal("103650")  # about half of 7185.90


# ----------------------------------------------------------------------
# Rate from the maturity amount (either one is enough for a cumulative FD)
# ----------------------------------------------------------------------

from financial_engine.fd_valuation import derive_fd_interest_rate  # noqa: E402


def test_rate_is_recovered_from_the_maturity_amount():
    assert derive_fd_interest_rate(principal=Decimal("100000"), maturity_value=Decimal("107185.90"),
                                   issue_date=ISSUE, maturity_date=MATURITY) == Decimal("7.00")
    # and round-trips through the projection
    rate = derive_fd_interest_rate(principal=Decimal("250000"), maturity_value=Decimal("300000"),
                                   issue_date=date(2024, 1, 1), maturity_date=date(2027, 1, 1))
    projected = compute_fd_current_value(
        principal=Decimal("250000"), interest_rate=rate, issue_date=date(2024, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY", maturity_value=None,
        as_of=date(2027, 1, 1),
    )
    assert abs(projected - Decimal("300000")) < Decimal("50")  # rate is rounded to 2 decimals


def test_rate_cannot_come_from_a_maturity_amount_at_or_below_principal():
    with pytest.raises(ValueError, match="more than the principal"):
        derive_fd_interest_rate(principal=Decimal("100000"), maturity_value=Decimal("100000"),
                                issue_date=ISSUE, maturity_date=MATURITY)


# ----------------------------------------------------------------------
# Result type and rounding
# ----------------------------------------------------------------------


def test_result_is_always_a_decimal_rounded_to_the_cent():
    for as_of in (ISSUE, date(2025, 5, 17), MATURITY):
        result = value(as_of=as_of, maturity_value=Decimal("107123.456"))
        assert isinstance(result, Decimal)
        assert result == result.quantize(Decimal("0.01"))


def test_zero_length_term_does_not_raise_and_returns_principal():
    # Guards the (schema-should-prevent) maturity_date == issue_date case.
    assert compute_fd_current_value(
        principal=Decimal("5000"), interest_rate=Decimal("6"),
        issue_date=ISSUE, maturity_date=ISSUE, payout_type="MATURITY",
        maturity_value=Decimal("5300"), as_of=ISSUE,
    ) == Decimal("5000.00")


# ----------------------------------------------------------------------
# Payout interest
# ----------------------------------------------------------------------

from financial_engine.fd_valuation import compute_fd_payout_interest  # noqa: E402


def test_quarterly_and_monthly_payout_amounts():
    p, r = Decimal("2500000"), Decimal("7.96")
    assert compute_fd_payout_interest(principal=p, interest_rate=r, payout_type="QUARTERLY") == Decimal("49750.00")
    assert compute_fd_payout_interest(principal=p, interest_rate=r, payout_type="MONTHLY") == Decimal("16583.33")
    assert compute_fd_payout_interest(principal=p, interest_rate=r, payout_type="MATURITY") is None


def test_monthly_payout_fd_value_stays_at_principal():
    assert compute_fd_current_value(
        principal=Decimal("100000"), interest_rate=Decimal("7"), issue_date=ISSUE,
        maturity_date=MATURITY, payout_type="MONTHLY", maturity_value=None,
        as_of=date(2025, 7, 1),
    ) == Decimal("100000.00")


# ----------------------------------------------------------------------
# Interest earned so far / total interest by maturity
# ----------------------------------------------------------------------

from financial_engine.fd_valuation import compute_fd_interest  # noqa: E402


def _interest(payout_type, as_of, **kw):
    args = dict(
        principal=Decimal("2500000"), interest_rate=Decimal("7.96"),
        issue_date=date(2025, 3, 14), maturity_date=date(2026, 9, 13),
        payout_type=payout_type, maturity_value=None, as_of=as_of,
    )
    args.update(kw)
    return compute_fd_interest(**args)


def test_payout_fd_interest_by_days_including_broken_period():
    earned, total = _interest("QUARTERLY", date(2025, 9, 14))
    assert earned == Decimal("100317.81")   # 184 days: 25L x 7.96% x 184 / 365
    assert total == Decimal("298772.60")    # 548 days: 5 quarters + a broken period


def test_payout_fd_interest_stops_at_maturity_and_is_zero_before_issue():
    earned, total = _interest("MONTHLY", date(2026, 12, 1))
    assert earned == total
    assert _interest("QUARTERLY", date(2025, 1, 1))[0] == Decimal("0.00")


def test_cumulative_fd_interest_is_growth_in_its_own_value():
    earned, total = _interest("MATURITY", date(2025, 12, 13), maturity_value=Decimal("2800000"))
    assert total == Decimal("300000")
    assert Decimal("0") < earned < total

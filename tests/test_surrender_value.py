"""Tests for `financial_engine.surrender_value` (estimates only)."""

from datetime import date
from decimal import Decimal

import pytest

from financial_engine.surrender_value import (
    default_gsv_factor_pct,
    estimate_traditional,
    estimate_ulip,
    policy_year_on,
)


def test_policy_year():
    assert policy_year_on(date(2020, 3, 1), date(2020, 3, 1)) == 1
    assert policy_year_on(date(2020, 3, 1), date(2021, 2, 28)) == 1
    assert policy_year_on(date(2020, 3, 1), date(2021, 3, 1)) == 2


@pytest.mark.parametrize("year, expected", [
    (1, "0"), (2, "30"), (3, "30"), (4, "50"), (7, "50"), (11, "63.33"), (19, "90"), (20, "90"),
])
def test_default_gsv_scale_for_a_20_year_policy(year, expected):
    # linear from 50% after year 7 to 90% at year 19 (= term - 1): year 11 -> 50 + 40*4/12
    assert default_gsv_factor_pct(year, 20) == Decimal(expected)


def _endowment(as_of, **kw):
    args = dict(
        start_date=date(2016, 1, 1), maturity_date=date(2036, 1, 1), as_of=as_of,
        premium_amount=Decimal("50000"), installments_per_year=1, premium_paying_term_years=20,
        installments_paid=10, sum_assured=Decimal("1000000"), accrued_bonus=Decimal("400000"),
    )
    args.update(kw)
    return estimate_traditional(**args)


def test_nothing_payable_before_two_full_years():
    est = _endowment(date(2017, 6, 1), installments_paid=1)
    assert est.estimate == 0 and "2 full years" in est.notes[0]


def test_gsv_ssv_and_estimate_is_the_higher():
    est = _endowment(date(2026, 1, 1), bonus_cash_value_factor=Decimal("0.2"))
    # policy year 11 of 20: GSV factor 50 + 40 * 4/12 = 63.33%
    assert est.policy_year == 11 and est.gsv_factor_pct == Decimal("63.33")
    assert est.premiums_paid == Decimal("500000")
    assert est.gsv == Decimal("396650.00")  # 316650 + 400000 x 0.2
    assert est.paid_up_value == Decimal("900000.00")  # 10L x 10/20 + 4L bonus
    # 3653 days (10.008 years) to maturity at 7%: 1 / 1.07^10.008 = 0.5082
    assert est.ssv_factor == Decimal("0.5082")
    assert est.ssv == Decimal("457380.00")
    assert est.estimate == est.ssv == max(est.gsv, est.ssv)


def test_bonus_left_out_of_gsv_without_its_factor_and_overrides_respected():
    est = _endowment(date(2026, 1, 1), gsv_factor_pct=Decimal("80"), ssv_factor=Decimal("0.3"))
    assert est.gsv == Decimal("400000.00")  # 80% of premiums, no bonus part
    assert any("bonus cash-value factor" in n for n in est.notes)
    assert est.ssv == Decimal("270000.00")  # 900000 x 0.3
    assert est.estimate == Decimal("400000.00")


def test_ulip_charges_small_and_large_premium_and_after_lock_in():
    small = estimate_ulip(start_date=date(2025, 1, 1), as_of=date(2025, 6, 1),
                          fund_value=Decimal("30000"), annual_premium=Decimal("24000"))
    assert small.discontinuance_charge == Decimal("3000.00")  # 20% of 24000 = 4800, capped 3000
    assert small.estimate == Decimal("27000.00")
    assert any("lock-in" in n for n in small.notes)
    large = estimate_ulip(start_date=date(2023, 1, 1), as_of=date(2025, 6, 1),
                          fund_value=Decimal("300000"), annual_premium=Decimal("100000"))
    assert large.policy_year == 3 and large.discontinuance_charge == Decimal("3000.00")  # 3% of 1L
    late = estimate_ulip(start_date=date(2018, 1, 1), as_of=date(2025, 6, 1),
                         fund_value=Decimal("500000"), annual_premium=Decimal("100000"))
    assert late.discontinuance_charge == 0 and late.estimate == Decimal("500000.00")
    assert not any("lock-in" in n for n in late.notes)

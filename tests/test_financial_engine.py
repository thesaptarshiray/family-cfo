from datetime import date
from decimal import Decimal

import pytest

from financial_engine import FinancialEngine
from repository.asset_repository import AssetRepository
from repository.bank_repository import BankRepository
from repository.database import Database
from repository.family_repository import FamilyRepository
from repository.insurance_repository import InsurancePolicyRepository
from repository.schema import initialize_schema
from repository.transaction_repository import TransactionRepository


@pytest.fixture
def engine_setup(tmp_path):
    database = Database(tmp_path / "engine.db")
    initialize_schema(database)

    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")

    bank_repo = BankRepository(database)
    from_account = bank_repo.create_account(
        member.member_id,
        "Example Bank",
        "SAVINGS",
        "1234",
        opening_balance=Decimal("1000.00"),
        ownership=[(member.member_id, Decimal("100"))],
    )
    to_account = bank_repo.create_account(
        member.member_id,
        "Example Bank",
        "CURRENT",
        "5678",
        opening_balance=Decimal("500.00"),
        ownership=[(member.member_id, Decimal("100"))],
    )

    return database, from_account, to_account


def test_create_transfer_updates_balances_and_writes_ledger(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)

    from_txn, to_txn = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
        description="Family transfer",
    )

    assert from_txn.amount == Decimal("-250.00")
    assert to_txn.amount == Decimal("250.00")
    assert from_txn.txn_type == "TRANSFER"
    assert to_txn.txn_type == "TRANSFER"
    assert from_txn.account_id == from_account.account_id
    assert to_txn.account_id == to_account.account_id
    assert engine.get_account_balance(from_account.account_id) == Decimal("750.00")
    assert engine.get_account_balance(to_account.account_id) == Decimal("750.00")

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT account_id, amount, transfer_group_id, status FROM \"transaction\" ORDER BY txn_id"
        ).fetchall()

    assert [row["account_id"] for row in rows] == [
        from_account.account_id,
        to_account.account_id,
    ]
    assert [Decimal(row["amount"]) for row in rows] == [
        Decimal("-250.00"),
        Decimal("250.00"),
    ]
    assert rows[0]["transfer_group_id"] == rows[1]["transfer_group_id"]
    assert rows[0]["status"] == "POSTED"
    assert rows[1]["status"] == "POSTED"


def test_create_transfer_rejects_non_positive_amount(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)

    with pytest.raises(ValueError, match="positive"):
        engine.create_transfer(
            from_account_id=from_account.account_id,
            to_account_id=to_account.account_id,
            amount=Decimal("0"),
        )


def test_execute_reversal_creates_inverse_transaction_and_updates_balance(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)

    engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
        description="Family transfer",
    )

    reversal = engine.execute_reversal(
        txn_id=1,
        reason="Entry correction",
    )

    assert reversal.amount == Decimal("250.00")
    assert reversal.txn_type == "TRANSFER"
    assert reversal.account_id == from_account.account_id
    assert reversal.description == "Reversal of transfer"
    assert engine.get_account_balance(from_account.account_id) == Decimal("1000.00")
    assert engine.get_account_balance(to_account.account_id) == Decimal("750.00")

    with database.connect() as conn:
        rows = conn.execute(
            "SELECT account_id, amount, reversal_of_txn_id, status FROM \"transaction\" ORDER BY txn_id"
        ).fetchall()

    assert [Decimal(row["amount"]) for row in rows] == [
        Decimal("-250.00"),
        Decimal("250.00"),
        Decimal("250.00"),
    ]
    assert rows[2]["reversal_of_txn_id"] == 1
    assert rows[2]["status"] == "REVERSED"


# ----------------------------------------------------------------------
# Ledger persistence goes through TransactionRepository, inside one
# Unit of Work per Engine operation
# ----------------------------------------------------------------------


def _ledger(database):
    conn = database.connect()
    try:
        return [
            (row["txn_id"], row["account_id"], Decimal(str(row["amount"])), row["status"])
            for row in conn.execute(
                'SELECT txn_id, account_id, amount, status FROM "transaction" ORDER BY txn_id'
            )
        ]
    finally:
        conn.close()


def test_transfer_and_reversal_persist_the_correct_cache_value(engine_setup):
    """`get_account_balance` recomputes from source and would pass even if the
    persisted `current_balance` cache held the wrong number. Check the cache
    itself, read independently through BankRepository, after both operations."""
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)
    bank_repo = BankRepository(database)

    from_txn, _ = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
    )
    assert bank_repo.get_account(from_account.account_id).current_balance == Decimal("750")
    assert bank_repo.get_account(to_account.account_id).current_balance == Decimal("750")

    engine.execute_reversal(txn_id=from_txn.txn_id, reason="Entry correction")
    assert bank_repo.get_account(from_account.account_id).current_balance == Decimal("1000")
    assert bank_repo.get_account(to_account.account_id).current_balance == Decimal("750")


def test_transfer_returns_the_rows_the_repository_stored(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)

    from_txn, to_txn = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
        description="Family transfer",
    )

    repo = TransactionRepository(database)
    assert repo.get_transaction(from_txn.txn_id) == from_txn
    assert repo.get_transaction(to_txn.txn_id) == to_txn
    assert from_txn.transfer_group_id == to_txn.transfer_group_id
    assert from_txn.transfer_group_id is not None
    assert (from_txn.status, to_txn.status) == ("POSTED", "POSTED")
    assert from_txn.amount == -to_txn.amount  # the two legs mirror each other


def test_reversal_returns_the_stored_row_and_marks_the_original(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)
    from_txn, _ = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
    )

    reversal = engine.execute_reversal(txn_id=from_txn.txn_id, reason="Entry correction")

    repo = TransactionRepository(database)
    assert repo.get_transaction(reversal.txn_id) == reversal
    assert reversal.reversal_of_txn_id == from_txn.txn_id
    assert reversal.status == "REVERSED"
    assert repo.get_transaction(from_txn.txn_id).status == "REVERSED"


def test_transfer_failing_on_the_second_leg_leaves_nothing_behind(engine_setup, monkeypatch):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)
    original = TransactionRepository.create_transaction
    calls = []

    def fail_on_second_leg(self, **kwargs):
        calls.append(kwargs["account_id"])
        if len(calls) == 2:
            raise RuntimeError("simulated failure on the second leg")
        return original(self, **kwargs)

    monkeypatch.setattr(TransactionRepository, "create_transaction", fail_on_second_leg)

    with pytest.raises(RuntimeError, match="second leg"):
        engine.create_transfer(
            from_account_id=from_account.account_id,
            to_account_id=to_account.account_id,
            amount=Decimal("250.00"),
        )

    assert len(calls) == 2  # the first leg really was written before the failure
    assert _ledger(database) == []
    assert engine.get_account_balance(from_account.account_id) == Decimal("1000.00")
    assert engine.get_account_balance(to_account.account_id) == Decimal("500.00")


def test_transfer_failing_after_both_legs_are_written_rolls_back_the_ledger(
    engine_setup, monkeypatch
):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)

    def fail_when_balances_are_recomputed(self, *args, **kwargs):
        raise RuntimeError("simulated failure while recomputing balances")

    # The balance recompute happens after both ledger rows are inserted.
    monkeypatch.setattr(TransactionRepository, "list_amounts", fail_when_balances_are_recomputed)

    with pytest.raises(RuntimeError, match="recomputing balances"):
        engine.create_transfer(
            from_account_id=from_account.account_id,
            to_account_id=to_account.account_id,
            amount=Decimal("250.00"),
        )

    assert _ledger(database) == []
    cached = BankRepository(database).get_account(from_account.account_id).current_balance
    assert cached == Decimal("0")  # the cache update never happened either


def test_reversal_failing_after_the_compensating_row_rolls_everything_back(
    engine_setup, monkeypatch
):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)
    from_txn, to_txn = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
    )
    before = _ledger(database)

    def fail_on_status_update(self, *args, **kwargs):
        raise RuntimeError("simulated failure while marking the original")

    monkeypatch.setattr(TransactionRepository, "update_transaction_status", fail_on_status_update)

    with pytest.raises(RuntimeError, match="marking the original"):
        engine.execute_reversal(txn_id=from_txn.txn_id, reason="Entry correction")

    assert _ledger(database) == before  # no compensating row, original still POSTED
    assert engine.get_account_balance(from_account.account_id) == Decimal("750.00")


def test_transfer_to_an_unknown_account_is_rejected_and_writes_nothing(engine_setup):
    database, from_account, _ = engine_setup
    engine = FinancialEngine(database)

    with pytest.raises(ValueError, match="must exist"):
        engine.create_transfer(
            from_account_id=from_account.account_id, to_account_id=999, amount=Decimal("10")
        )

    assert _ledger(database) == []


def test_reversal_validation_is_preserved(engine_setup):
    database, from_account, to_account = engine_setup
    engine = FinancialEngine(database)
    from_txn, _ = engine.create_transfer(
        from_account_id=from_account.account_id,
        to_account_id=to_account.account_id,
        amount=Decimal("250.00"),
    )

    with pytest.raises(ValueError, match="reason is required"):
        engine.execute_reversal(txn_id=from_txn.txn_id, reason="   ")
    with pytest.raises(ValueError, match="does not exist"):
        engine.execute_reversal(txn_id=999, reason="Entry correction")

    engine.execute_reversal(txn_id=from_txn.txn_id, reason="Entry correction")
    with pytest.raises(ValueError, match="only posted"):
        engine.execute_reversal(txn_id=from_txn.txn_id, reason="Again")


def test_balance_of_an_unknown_account_is_rejected(engine_setup):
    database, _, _ = engine_setup

    with pytest.raises(ValueError, match="account 999 does not exist"):
        FinancialEngine(database).get_account_balance(999)


# ----------------------------------------------------------------------
# FD domain: create, read, and portfolio aggregation
# ----------------------------------------------------------------------


@pytest.fixture
def family_setup(tmp_path):
    """A family with two members, and no bank accounts (FD tests don't need one)."""
    database = Database(tmp_path / "fd_engine.db")
    initialize_schema(database)
    engine = FinancialEngine(database)
    family = engine.create_family("Sharma")
    member = engine.create_family_member(family.family_id, "Raj", "self")
    spouse = engine.create_family_member(family.family_id, "Asha", "spouse")
    return engine, family, member, spouse


def test_create_fd_persists_and_computes_current_value_atomically(family_setup):
    engine, _, member, _ = family_setup

    fd = engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-1",
        principal=Decimal("100000"), interest_rate=Decimal("7.0"),
        issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
        payout_type="MATURITY", maturity_value=Decimal("107000"),
    )

    assert fd.bank_name == "HDFC"
    assert fd.principal == Decimal("100000")
    assert fd.current_value > Decimal("0")  # actually computed, not the schema default
    assert fd.gain_loss == fd.current_value - fd.principal

    # Persisted: the asset and fd_holding rows exist, and the cache was set.
    asset_repo = AssetRepository(engine._db)
    stored_asset = asset_repo.get_asset(fd.asset_id)
    assert stored_asset.category == "FD"
    assert stored_asset.invested_amount == Decimal("100000")
    assert stored_asset.current_value == fd.current_value  # cache set at creation
    assert asset_repo.get_fd(fd.asset_id).fd_number == "FD-1"


def test_create_fd_defaults_name_and_institution_from_bank_and_fd_number(family_setup):
    engine, _, member, _ = family_setup

    fd = engine.create_fd(
        member_id=member.member_id, bank_name="SBI", fd_number="FD-2",
        principal=10000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY",
    )

    assert fd.name == "SBI FD FD-2"


def test_create_fd_accepts_an_explicit_name(family_setup):
    engine, _, member, _ = family_setup

    fd = engine.create_fd(
        member_id=member.member_id, bank_name="SBI", fd_number="FD-3",
        principal=10000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY",
        name="Emergency fund FD",
    )

    assert fd.name == "Emergency fund FD"


def test_create_fd_rejects_invalid_terms_and_writes_nothing(family_setup):
    engine, _, member, _ = family_setup

    with pytest.raises(Exception):
        engine.create_fd(
            member_id=member.member_id, bank_name="SBI", fd_number="FD-4",
            principal=10000, interest_rate=6, issue_date=date(2026, 1, 1),
            maturity_date=date(2026, 1, 1),  # not after issue_date
            payout_type="MATURITY",
        )

    assert engine.list_fds(member.member_id) == []


def test_list_fds_returns_only_that_members_fds_with_a_fresh_value(family_setup):
    engine, _, member, spouse = family_setup
    engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-A",
        principal=100000, interest_rate=7, issue_date=date(2020, 1, 1),
        maturity_date=date(2021, 1, 1), payout_type="MATURITY",
        maturity_value=Decimal("107000"),
    )
    engine.create_fd(
        member_id=spouse.member_id, bank_name="SBI", fd_number="FD-B",
        principal=50000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY",
    )

    fds = engine.list_fds(member.member_id)
    assert len(fds) == 1
    assert fds[0].fd_number == "FD-A"
    # matured years ago: a fresh read reflects the matured value, not a stale
    # creation-time cache.
    assert fds[0].current_value == Decimal("107000.00")


def test_list_fds_for_unknown_member_is_empty(family_setup):
    engine, _, _, _ = family_setup
    assert engine.list_fds(999) == []


def test_member_investment_summary_sums_that_members_fds_only(family_setup):
    engine, _, member, spouse = family_setup
    engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-1",
        principal=100000, interest_rate=7, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY",
        maturity_value=Decimal("107000"),
    )
    engine.create_fd(
        member_id=member.member_id, bank_name="ICICI", fd_number="FD-2",
        principal=20000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2028, 1, 1), payout_type="QUARTERLY",
    )
    engine.create_fd(
        member_id=spouse.member_id, bank_name="SBI", fd_number="FD-3",
        principal=999999, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="MATURITY",
    )

    summary = engine.get_member_investment_summary(member.member_id)

    assert summary.name == "Raj"
    assert len(summary.fds) == 2
    assert summary.total_invested == Decimal("120000")
    assert summary.total_current_value == summary.total_invested + summary.total_gain_loss
    # the spouse's much larger FD must not leak into this member's totals
    assert summary.total_invested < Decimal("999999")


def test_member_investment_summary_of_unknown_member_is_rejected(family_setup):
    engine, _, _, _ = family_setup
    with pytest.raises(ValueError, match="family member 999 does not exist"):
        engine.get_member_investment_summary(999)


def test_family_dashboard_aggregates_every_members_holdings(family_setup):
    engine, family, member, spouse = family_setup
    engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-1",
        principal=100000, interest_rate=7, issue_date=date(2020, 1, 1),
        maturity_date=date(2021, 1, 1), payout_type="MATURITY",
        maturity_value=Decimal("107000"),  # matured years ago: value is exact
    )
    engine.create_fd(
        member_id=spouse.member_id, bank_name="SBI", fd_number="FD-2",
        principal=50000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="QUARTERLY",
    )

    dashboard = engine.get_family_dashboard(family.family_id)

    assert dashboard.family_name == "Sharma"
    assert dashboard.total_invested == Decimal("150000")
    assert dashboard.total_current_value == Decimal("157000.00")
    assert dashboard.total_gain_loss == dashboard.total_current_value - dashboard.total_invested
    assert dashboard.by_category == {"FD": dashboard.total_current_value}
    assert {m.member_id for m in dashboard.members} == {member.member_id, spouse.member_id}


def test_family_dashboard_with_no_holdings_yet_is_all_zero(family_setup):
    engine, family, _, _ = family_setup

    dashboard = engine.get_family_dashboard(family.family_id)

    assert dashboard.total_invested == Decimal("0")
    assert dashboard.total_current_value == Decimal("0")
    assert dashboard.by_category == {}
    assert len(dashboard.members) == 2
    assert all(m.total_invested == Decimal("0") for m in dashboard.members)


def test_family_dashboard_of_unknown_family_is_rejected(family_setup):
    engine, _, _, _ = family_setup
    with pytest.raises(ValueError, match="family 999 does not exist"):
        engine.get_family_dashboard(999)


# ----------------------------------------------------------------------
# Insurance domain: create, read, and its schedule projection
# ----------------------------------------------------------------------


def test_create_insurance_policy_computes_its_schedule(family_setup):
    engine, _, member, _ = family_setup

    policy = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-1", premium_amount=Decimal("12000"),
        premium_frequency="ANNUAL", start_date=date(2020, 1, 15),
        sum_assured=Decimal("5000000"), maturity_date=date(2050, 1, 15),
    )

    assert policy.insurer == "LIC"
    assert policy.sum_assured == Decimal("5000000")
    assert policy.installments_due_so_far > 0
    assert policy.total_premium_due_to_date == policy.premium_amount * policy.installments_due_so_far
    assert policy.next_due_date is not None

    stored = InsurancePolicyRepository(engine._db).get_policy(policy.policy_id)
    assert stored.premium_amount == Decimal("12000")
    assert stored.status == "ACTIVE"


def test_create_insurance_policy_with_minimal_fields(family_setup):
    engine, _, member, _ = family_setup

    policy = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="HEALTH", insurer="Example Insurer",
        policy_number="POL-2", premium_amount=8000, premium_frequency="MONTHLY",
        start_date=date(2026, 1, 1),
    )

    assert policy.sum_assured is None
    assert policy.maturity_date is None
    assert policy.next_due_date is not None  # open-ended, still active


def test_create_insurance_policy_rejects_invalid_terms_and_writes_nothing(family_setup):
    engine, _, member, _ = family_setup

    with pytest.raises(Exception):
        engine.create_insurance_policy(
            member_id=member.member_id, policy_type="TRAVEL",  # not a valid policy_type
            insurer="Example Insurer", policy_number="POL-3",
            premium_amount=1000, premium_frequency="ANNUAL", start_date=date(2026, 1, 1),
        )

    assert engine.list_insurance_policies(member.member_id) == []


def test_list_insurance_policies_returns_only_that_members_policies(family_setup):
    engine, _, member, spouse = family_setup
    engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-A", premium_amount=12000, premium_frequency="ANNUAL",
        start_date=date(2026, 1, 1),
    )
    engine.create_insurance_policy(
        member_id=spouse.member_id, policy_type="HEALTH", insurer="Example Insurer",
        policy_number="POL-B", premium_amount=8000, premium_frequency="MONTHLY",
        start_date=date(2026, 1, 1),
    )

    policies = engine.list_insurance_policies(member.member_id)
    assert len(policies) == 1
    assert policies[0].policy_number == "POL-A"


def test_member_investment_summary_includes_insurance_without_affecting_totals(family_setup):
    engine, _, member, _ = family_setup
    engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-1", premium_amount=12000, premium_frequency="ANNUAL",
        start_date=date(2020, 1, 1),
    )
    engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-1",
        principal=100000, interest_rate=7, issue_date=date(2025, 1, 1),
        maturity_date=date(2026, 1, 1), payout_type="MATURITY",
        maturity_value=Decimal("107000"),
    )

    summary = engine.get_member_investment_summary(member.member_id)

    assert len(summary.insurance_policies) == 1
    assert summary.insurance_policies[0].policy_number == "POL-1"
    # the policy's premium obligation must not leak into investment totals
    assert summary.total_invested == Decimal("100000")
    assert summary.total_current_value == Decimal("107000.00")


def test_family_dashboard_includes_insurance_per_member_but_not_in_totals(family_setup):
    engine, family, member, spouse = family_setup
    engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-1", premium_amount=999999, premium_frequency="ANNUAL",
        start_date=date(2020, 1, 1),
    )
    engine.create_fd(
        member_id=spouse.member_id, bank_name="SBI", fd_number="FD-1",
        principal=50000, interest_rate=6, issue_date=date(2026, 1, 1),
        maturity_date=date(2027, 1, 1), payout_type="QUARTERLY",
    )

    dashboard = engine.get_family_dashboard(family.family_id)

    member_summary = next(m for m in dashboard.members if m.member_id == member.member_id)
    assert len(member_summary.insurance_policies) == 1
    assert dashboard.total_invested == Decimal("50000")  # the FD only
    assert "TERM" not in dashboard.by_category  # by_category stays asset-only


def test_insurance_policy_for_unknown_member_is_rejected(family_setup):
    engine, _, _, _ = family_setup
    with pytest.raises(Exception):
        engine.create_insurance_policy(
            member_id=999, policy_type="TERM", insurer="LIC", policy_number="POL-X",
            premium_amount=1000, premium_frequency="ANNUAL", start_date=date(2026, 1, 1),
        )


# ----------------------------------------------------------------------
# MF domain: transaction-based holdings, valuation, gain/loss, XIRR
# ----------------------------------------------------------------------


def _create_mf(engine, member_id, **overrides):
    fields = dict(
        member_id=member_id, amc="Example AMC", scheme_name="Balanced Growth",
        folio_number="FOLIO-1", initial_txn_date=date(2025, 1, 15),
        initial_txn_type="SIP", initial_units=Decimal("10.5"),
        initial_nav=Decimal("95.2381"), initial_amount=Decimal("1000.00"),
    )
    fields.update(overrides)
    return engine.create_mutual_fund(**fields)


def test_create_mutual_fund_persists_units_and_invested_amount_atomically(family_setup):
    engine, _, member, _ = family_setup

    mf = _create_mf(engine, member.member_id, isin="INF000000001")

    assert mf.scheme_name == "Balanced Growth"
    assert mf.units == Decimal("10.5")
    assert mf.invested_amount == Decimal("1000.00")
    assert mf.avg_nav == Decimal("95.2381")
    assert len(mf.transactions) == 1

    stored_asset = AssetRepository(engine._db).get_asset(mf.asset_id)
    assert stored_asset.category == "MF"
    assert stored_asset.invested_amount == Decimal("1000.00")  # cache set at creation
    stored_holding = AssetRepository(engine._db).get_mutual_fund(mf.asset_id)
    assert stored_holding.units == Decimal("10.5")  # cache set at creation


def test_create_mutual_fund_seeds_a_valuation_from_the_first_transaction(family_setup):
    engine, _, member, _ = family_setup

    mf = _create_mf(engine, member.member_id)

    assert mf.current_nav == Decimal("95.2381")
    assert mf.current_nav_source == "MANUAL"
    assert mf.current_nav_date == date(2025, 1, 15)
    assert mf.current_value == mf.units * mf.current_nav


def test_create_mutual_fund_rejects_invalid_transaction_and_writes_nothing(family_setup):
    engine, _, member, _ = family_setup

    with pytest.raises(Exception):
        _create_mf(engine, member.member_id, initial_units=Decimal("0"))  # CHECK units > 0

    assert engine.list_mfs(member.member_id) == []
    assert AssetRepository(engine._db).list_assets(member.member_id) == []


def test_record_mf_transaction_recomputes_units_avg_nav_and_invested_amount(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)

    updated = engine.record_mf_transaction(
        asset_id=mf.asset_id, txn_date=date(2025, 2, 15), txn_type="SIP",
        units=Decimal("9.8"), nav=Decimal("102.04"), amount=Decimal("1000.00"),
        description="February SIP",
    )

    assert updated.units == Decimal("20.3")
    assert updated.invested_amount == Decimal("2000.00")
    assert len(updated.transactions) == 2
    # weighted average: (10.5*95.2381 + 9.8*102.04) / 20.3
    expected_avg = (
        (Decimal("10.5") * Decimal("95.2381") + Decimal("9.8") * Decimal("102.04"))
        / Decimal("20.3")
    ).quantize(Decimal("0.0001"))
    assert updated.avg_nav == expected_avg


def test_record_mf_transaction_updates_current_nav_when_more_recent(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)  # NAV 95.2381 on 2025-01-15

    updated = engine.record_mf_transaction(
        asset_id=mf.asset_id, txn_date=date(2025, 2, 15), txn_type="SIP",
        units=Decimal("9.8"), nav=Decimal("102.04"), amount=Decimal("1000.00"),
    )

    assert updated.current_nav == Decimal("102.04")  # the later transaction's NAV
    assert updated.current_nav_date == date(2025, 2, 15)


def test_record_mf_transaction_for_unknown_asset_is_rejected(family_setup):
    engine, _, _, _ = family_setup
    with pytest.raises(ValueError, match="MF holding 999 does not exist"):
        engine.record_mf_transaction(
            asset_id=999, txn_date=date(2026, 1, 1), txn_type="SIP",
            units=1, nav=100, amount=100,
        )


def test_record_mf_transaction_rejects_invalid_terms_and_writes_nothing(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)

    with pytest.raises(Exception):
        engine.record_mf_transaction(
            asset_id=mf.asset_id, txn_date=date(2025, 2, 15), txn_type="REDEMPTION",  # invalid
            units=1, nav=100, amount=100,
        )

    unchanged = engine.list_mfs(member.member_id)[0]
    assert len(unchanged.transactions) == 1  # the invalid one did not get added
    assert unchanged.units == Decimal("10.5")


def test_record_mf_valuation_refreshes_current_value_without_adding_a_transaction(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)

    updated = engine.record_mf_valuation(
        asset_id=mf.asset_id, valuation_date=date(2026, 6, 1), nav=Decimal("115.50")
    )

    assert updated.current_nav == Decimal("115.50")
    assert updated.current_nav_date == date(2026, 6, 1)
    assert updated.current_value == updated.units * Decimal("115.50")
    assert len(updated.transactions) == 1  # unchanged: no new transaction
    assert updated.units == Decimal("10.5")  # unchanged


def test_record_mf_valuation_on_the_same_date_as_a_transaction_corrects_it(family_setup):
    """The transaction auto-seeded a valuation for that date; an explicit,
    user-initiated entry for the same date is a deliberate correction (the
    schema's own mechanism for this), not rejected and not silently
    discarded in favour of the auto-seeded one."""
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)  # seeds a valuation on 2025-01-15 at 95.2381

    updated = engine.record_mf_valuation(
        asset_id=mf.asset_id, valuation_date=date(2025, 1, 15), nav=Decimal("99")
    )

    assert updated.current_nav == Decimal("99")  # the correction, not the auto-seeded 95.2381


def test_record_mf_valuation_twice_on_the_same_date_the_second_call_wins(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)

    engine.record_mf_valuation(asset_id=mf.asset_id, valuation_date=date(2026, 6, 1), nav=100)
    updated = engine.record_mf_valuation(
        asset_id=mf.asset_id, valuation_date=date(2026, 6, 1), nav=105
    )

    assert updated.current_nav == Decimal("105")


def test_gain_loss_reflects_current_value_versus_invested_amount(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)

    updated = engine.record_mf_valuation(
        asset_id=mf.asset_id, valuation_date=date(2026, 6, 1), nav=Decimal("115.50")
    )

    assert updated.gain_loss == updated.current_value - updated.invested_amount
    assert updated.gain_loss > 0
    assert updated.gain_loss_pct > 0


def test_xirr_is_computed_once_there_is_a_transaction_and_a_current_value(family_setup):
    engine, _, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)
    updated = engine.record_mf_valuation(
        asset_id=mf.asset_id, valuation_date=date(2026, 1, 15), nav=Decimal("115.50")
    )
    assert updated.xirr is not None


def test_list_mfs_returns_only_that_members_holdings(family_setup):
    engine, _, member, spouse = family_setup
    _create_mf(engine, member.member_id, folio_number="FOLIO-A")
    _create_mf(engine, spouse.member_id, folio_number="FOLIO-B")

    mfs = engine.list_mfs(member.member_id)
    assert len(mfs) == 1
    assert mfs[0].folio_number == "FOLIO-A"


def test_member_investment_summary_includes_mf_in_totals_unlike_insurance(family_setup):
    engine, _, member, _ = family_setup
    _create_mf(engine, member.member_id)
    engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-1", premium_amount=12000, premium_frequency="ANNUAL",
        start_date=date(2020, 1, 1),
    )

    summary = engine.get_member_investment_summary(member.member_id)

    assert len(summary.mfs) == 1
    assert summary.total_invested == Decimal("1000.00")  # MF only, not the insurance premium
    assert summary.total_current_value == summary.mfs[0].current_value


def test_family_dashboard_includes_mf_under_by_category(family_setup):
    engine, family, member, _ = family_setup
    _create_mf(engine, member.member_id)

    dashboard = engine.get_family_dashboard(family.family_id)

    assert "MF" in dashboard.by_category
    assert dashboard.by_category["MF"] == dashboard.total_current_value


def test_fd_and_mf_together_both_count_toward_the_same_totals(family_setup):
    engine, family, member, _ = family_setup
    _create_mf(engine, member.member_id)
    engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="FD-1",
        principal=100000, interest_rate=7, issue_date=date(2020, 1, 1),
        maturity_date=date(2021, 1, 1), payout_type="MATURITY",
        maturity_value=Decimal("107000"),
    )

    dashboard = engine.get_family_dashboard(family.family_id)

    assert set(dashboard.by_category) == {"MF", "FD"}
    assert dashboard.total_current_value == (
        dashboard.by_category["MF"] + dashboard.by_category["FD"]
    )


# ----------------------------------------------------------------------
# MF portfolio: family-wide and member-wise views
# ----------------------------------------------------------------------


def test_mf_portfolio_family_wide_lists_every_members_holdings_with_owner(family_setup):
    engine, family, member, spouse = family_setup
    _create_mf(engine, member.member_id, folio_number="F-RAJ")
    _create_mf(engine, spouse.member_id, folio_number="F-ASHA")

    portfolio = engine.get_mf_portfolio(family.family_id)

    assert portfolio.member_id is None
    assert {(r.owner_name, r.mf.folio_number) for r in portfolio.rows} == {
        ("Raj", "F-RAJ"), ("Asha", "F-ASHA"),
    }
    assert portfolio.total_invested == Decimal("2000.00")
    assert portfolio.total_current_value == sum(r.mf.current_value for r in portfolio.rows)


def test_mf_portfolio_member_wise_narrows_to_that_member(family_setup):
    engine, family, member, spouse = family_setup
    _create_mf(engine, member.member_id, folio_number="F-RAJ")
    _create_mf(engine, spouse.member_id, folio_number="F-ASHA")

    portfolio = engine.get_mf_portfolio(family.family_id, spouse.member_id)

    assert portfolio.member_id == spouse.member_id
    assert [r.mf.folio_number for r in portfolio.rows] == ["F-ASHA"]
    assert portfolio.total_invested == Decimal("1000.00")


def test_mf_portfolio_totals_and_combined_xirr(family_setup):
    engine, family, member, spouse = family_setup
    a = _create_mf(engine, member.member_id, folio_number="F-1")
    b = _create_mf(engine, spouse.member_id, folio_number="F-2")
    engine.record_mf_valuation(asset_id=a.asset_id, valuation_date=date(2026, 6, 1), nav=110)
    engine.record_mf_valuation(asset_id=b.asset_id, valuation_date=date(2026, 6, 1), nav=120)

    portfolio = engine.get_mf_portfolio(family.family_id)

    assert portfolio.total_gain_loss == portfolio.total_current_value - portfolio.total_invested
    assert portfolio.total_gain_loss > 0
    assert portfolio.total_gain_loss_pct > 0
    assert portfolio.xirr is not None and portfolio.xirr > 0


def test_mf_portfolio_of_a_family_with_no_mfs_is_empty_with_zero_totals(family_setup):
    engine, family, _, _ = family_setup
    portfolio = engine.get_mf_portfolio(family.family_id)
    assert portfolio.rows == ()
    assert portfolio.total_invested == Decimal("0")
    assert portfolio.xirr is None


def test_mf_portfolio_rejects_an_unknown_family_or_an_outside_member(family_setup, tmp_path):
    engine, family, _, _ = family_setup
    other_family = engine.create_family("Other")
    outsider = engine.create_family_member(other_family.family_id, "Stranger", "self")

    with pytest.raises(ValueError, match="family 999 does not exist"):
        engine.get_mf_portfolio(999)
    with pytest.raises(ValueError, match="is not in family"):
        engine.get_mf_portfolio(family.family_id, outsider.member_id)


# ----------------------------------------------------------------------
# FD and insurance portfolios: family-wide and member-wise views
# ----------------------------------------------------------------------


def _fd(engine, member_id, fd_number, principal, maturity_date, payout_type="MATURITY", **kw):
    return engine.create_fd(
        member_id=member_id, bank_name="HDFC", fd_number=fd_number,
        principal=Decimal(principal), interest_rate=Decimal("7"),
        issue_date=date(2025, 1, 1), maturity_date=maturity_date,
        payout_type=payout_type, **kw,
    )


def test_fd_portfolio_family_wide_totals_and_owners(family_setup):
    engine, family, member, spouse = family_setup
    _fd(engine, member.member_id, "FD-RAJ", "100000", date(2030, 1, 1),
        maturity_value=Decimal("150000"))
    _fd(engine, spouse.member_id, "FD-ASHA", "50000", date(2028, 6, 1), payout_type="QUARTERLY")

    p = engine.get_fd_portfolio(family.family_id)

    assert {(r.owner_name, r.fd.fd_number) for r in p.rows} == {("Raj", "FD-RAJ"), ("Asha", "FD-ASHA")}
    assert p.total_principal == Decimal("150000")
    assert p.total_current_value == sum(r.fd.current_value for r in p.rows)
    assert p.total_gain_loss == p.total_current_value - p.total_principal
    # cumulative FD at its quoted maturity value; payout FD stays at principal
    assert p.total_value_at_maturity == Decimal("200000")
    assert p.next_maturity.fd.fd_number == "FD-ASHA"


def test_fd_portfolio_by_bank_and_upcoming_maturities(family_setup):
    engine, family, member, spouse = family_setup
    _fd(engine, member.member_id, "A", "100000", date(2030, 1, 1), maturity_value=Decimal("150000"))
    _fd(engine, spouse.member_id, "B", "50000", date(2028, 6, 1))
    engine.create_fd(
        member_id=member.member_id, bank_name="SBI", fd_number="C", principal=Decimal("20000"),
        interest_rate=Decimal("6"), issue_date=date(2018, 1, 1), maturity_date=date(2020, 1, 1),
        payout_type="MATURITY",
    )

    p = engine.get_fd_portfolio(family.family_id)

    hdfc, sbi = p.by_bank
    assert (hdfc.bank_name, hdfc.principal) == ("HDFC", Decimal("150000"))
    assert hdfc.amount_at_maturity == sum(
        r.fd.principal + r.fd.total_interest_by_maturity for r in p.rows if r.fd.bank_name == "HDFC"
    )
    assert sbi.bank_name == "SBI"
    # already matured FDs are not upcoming
    assert [r.fd.fd_number for r in p.upcoming_maturities] == ["B", "A"]


def test_fd_portfolio_member_wise_narrows_to_that_member(family_setup):
    engine, family, member, spouse = family_setup
    _fd(engine, member.member_id, "FD-RAJ", "100000", date(2030, 1, 1))
    _fd(engine, spouse.member_id, "FD-ASHA", "50000", date(2028, 6, 1))

    p = engine.get_fd_portfolio(family.family_id, member.member_id)

    assert [r.fd.fd_number for r in p.rows] == ["FD-RAJ"]
    assert p.total_principal == Decimal("100000")


def test_fd_portfolio_next_maturity_skips_already_matured_fds(family_setup):
    engine, family, member, _ = family_setup
    engine.create_fd(
        member_id=member.member_id, bank_name="SBI", fd_number="OLD",
        principal=Decimal("10000"), interest_rate=Decimal("6"),
        issue_date=date(2018, 1, 1), maturity_date=date(2020, 1, 1), payout_type="MATURITY",
    )
    p = engine.get_fd_portfolio(family.family_id)
    assert p.next_maturity is None
    assert p.total_principal == Decimal("10000")


def test_fd_portfolio_empty_and_rejects_outside_member(family_setup):
    engine, family, _, _ = family_setup
    p = engine.get_fd_portfolio(family.family_id)
    assert p.rows == () and p.total_principal == Decimal("0") and p.next_maturity is None
    outsider = engine.create_family_member(engine.create_family("Other").family_id, "X", "self")
    with pytest.raises(ValueError, match="is not in family"):
        engine.get_fd_portfolio(family.family_id, outsider.member_id)
    with pytest.raises(ValueError, match="does not exist"):
        engine.get_insurance_portfolio(999)


def test_insurance_portfolio_totals_cover_and_yearly_premium(family_setup):
    engine, family, member, spouse = family_setup
    annual = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC", policy_number="P-1",
        premium_amount=Decimal("12000"), premium_frequency="ANNUAL", start_date=date(2020, 1, 15),
        sum_assured=Decimal("5000000"), maturity_date=date(2050, 1, 15),
    )
    monthly = engine.create_insurance_policy(
        member_id=spouse.member_id, policy_type="HEALTH", insurer="Star", policy_number="P-2",
        premium_amount=Decimal("1000"), premium_frequency="MONTHLY", start_date=date(2024, 3, 10),
        sum_assured=Decimal("500000"), current_surrender_value=Decimal("2500"),
    )

    p = engine.get_insurance_portfolio(family.family_id)

    assert {(r.owner_name, r.policy.policy_number) for r in p.rows} == {("Raj", "P-1"), ("Asha", "P-2")}
    assert p.total_sum_assured == Decimal("5500000")
    assert p.total_annual_premium == Decimal("24000")  # 12000 x1 + 1000 x12
    assert p.total_premium_due_to_date == (
        annual.total_premium_due_to_date + monthly.total_premium_due_to_date
    )
    assert p.total_surrender_value == Decimal("2500")
    soonest = min((annual, monthly), key=lambda pol: pol.next_due_date)
    assert p.next_premium.policy.policy_id == soonest.policy_id


def test_insurance_portfolio_member_wise(family_setup):
    engine, family, member, spouse = family_setup
    engine.create_insurance_policy(
        member_id=spouse.member_id, policy_type="TERM", insurer="LIC", policy_number="P-9",
        premium_amount=Decimal("2500"), premium_frequency="QUARTERLY", start_date=date(2024, 1, 1),
    )
    assert engine.get_insurance_portfolio(family.family_id, member.member_id).rows == ()
    p = engine.get_insurance_portfolio(family.family_id, spouse.member_id)
    assert p.total_annual_premium == Decimal("10000")
    assert p.total_sum_assured == Decimal("0")  # no sum assured recorded


# ----------------------------------------------------------------------
# Insurance bonus and asset allocation (Overview pies)
# ----------------------------------------------------------------------


def _policy(engine, member_id, policy_type="ENDOWMENT", number="P-1", **kw):
    return engine.create_insurance_policy(
        member_id=member_id, policy_type=policy_type, insurer="LIC", policy_number=number,
        premium_amount=Decimal("10000"), premium_frequency="ANNUAL",
        start_date=date(2022, 1, 1), **kw,
    )


def test_insurance_values_update_and_bonus_totals(family_setup):
    engine, family, member, spouse = family_setup
    a = _policy(engine, member.member_id, accrued_bonus=Decimal("4000"))
    b = _policy(engine, spouse.member_id, number="P-2")
    assert a.accrued_bonus == Decimal("4000") and a.is_savings_policy

    updated = engine.update_insurance_values(
        b.policy_id, current_surrender_value=Decimal("15000"), accrued_bonus=Decimal("2500")
    )
    assert updated.current_surrender_value == Decimal("15000")
    assert updated.accrued_bonus == Decimal("2500")

    p = engine.get_insurance_portfolio(family.family_id)
    assert p.total_accrued_bonus == Decimal("6500")
    assert p.total_surrender_value == Decimal("15000")

    cleared = engine.update_insurance_values(b.policy_id, current_surrender_value=None, accrued_bonus=None)
    assert cleared.accrued_bonus is None and cleared.current_surrender_value is None


def test_insurance_values_reject_negatives_and_unknown_policy(family_setup):
    engine, _, member, _ = family_setup
    pol = _policy(engine, member.member_id)
    with pytest.raises(ValueError, match="cannot be negative"):
        engine.update_insurance_values(pol.policy_id, current_surrender_value=Decimal("-1"), accrued_bonus=None)
    with pytest.raises(ValueError, match="does not exist"):
        engine.update_insurance_values(999, current_surrender_value=None, accrued_bonus=None)


def test_asset_allocation_splits_by_class_and_counts_only_savings_insurance(family_setup):
    engine, family, member, spouse = family_setup
    _fd(engine, member.member_id, "FD-1", "100000", date(2030, 1, 1))
    _create_mf(engine, spouse.member_id)
    savings = _policy(engine, spouse.member_id, current_surrender_value=Decimal("20000"),
                      accrued_bonus=Decimal("5000"))
    _policy(engine, member.member_id, policy_type="TERM", number="T-1")  # protection: excluded

    a = engine.get_asset_allocation(family.family_id)

    by_cat = {s.category: s for s in a.slices}
    assert set(by_cat) == {"FD", "MF", "INSURANCE"}
    assert by_cat["INSURANCE"].invested == savings.total_premium_due_to_date
    assert by_cat["INSURANCE"].current_value == Decimal("25000")
    assert a.total_invested == sum(s.invested for s in a.slices)
    assert a.total_current_value == sum(s.current_value for s in a.slices)
    assert a.total_gain_loss == a.total_current_value - a.total_invested
    assert abs(sum(s.current_value_pct for s in a.slices) - 100) <= Decimal("0.03")
    assert abs(sum(s.invested_pct for s in a.slices) - 100) <= Decimal("0.03")
    assert [s.current_value for s in a.slices] == sorted((s.current_value for s in a.slices), reverse=True)
    assert {m.name: m.invested for m in a.members}["Raj"] == Decimal("100000")
    assert sum(m.current_value for m in a.members) == a.total_current_value


def test_asset_allocation_member_wise_and_empty(family_setup):
    engine, family, member, spouse = family_setup
    empty = engine.get_asset_allocation(family.family_id)
    assert empty.slices == () and empty.total_invested == Decimal("0")

    _fd(engine, member.member_id, "FD-1", "100000", date(2030, 1, 1))
    _create_mf(engine, spouse.member_id)
    a = engine.get_asset_allocation(family.family_id, spouse.member_id)
    assert [s.category for s in a.slices] == ["MF"]
    assert a.slices[0].current_value_pct == Decimal("100.00")
    assert [m.name for m in a.members] == ["Asha"]


# ----------------------------------------------------------------------
# Stocks
# ----------------------------------------------------------------------


def _stock(engine, member_id, symbol="infy", qty="10", price="1500", **kw):
    return engine.create_stock(
        member_id=member_id, broker="Zerodha", symbol=symbol, exchange="NSE",
        initial_txn_date=date(2025, 1, 10), initial_quantity=Decimal(qty),
        initial_price=Decimal(price), **kw,
    )


def test_create_stock_buys_and_values_at_the_buy_price(family_setup):
    engine, _, member, _ = family_setup
    st = _stock(engine, member.member_id, initial_charges=Decimal("15"))
    assert st.symbol == "INFY" and st.name == "INFY"
    assert st.quantity == Decimal("10")
    assert st.invested_amount == Decimal("15015.00")
    assert st.current_price == Decimal("1500")
    assert st.current_value == Decimal("15000.00")
    holding = AssetRepository(engine._db).get_stock(st.asset_id)
    assert holding.quantity == Decimal("10")  # derived cache persisted


def test_stock_sell_realises_gain_and_price_update_revalues(family_setup):
    engine, _, member, _ = family_setup
    st = _stock(engine, member.member_id)
    engine.record_stock_transaction(asset_id=st.asset_id, txn_date=date(2025, 3, 1),
                                    txn_type="BUY", quantity=Decimal("10"), price=Decimal("1700"))
    st = engine.record_stock_transaction(asset_id=st.asset_id, txn_date=date(2025, 6, 1),
                                         txn_type="SELL", quantity=Decimal("5"), price=Decimal("1800"))
    assert st.quantity == Decimal("15")
    assert st.avg_buy_price == Decimal("1600.0000")
    assert st.realised_gain == Decimal("1000.00")
    st = engine.record_stock_price(asset_id=st.asset_id, price_date=date(2026, 9, 1), price=Decimal("2000"))
    assert st.current_value == Decimal("30000.00")
    assert st.gain_loss == Decimal("6000.00")
    assert st.xirr is not None and st.xirr > 0


def test_overselling_a_stock_is_rejected_and_nothing_is_saved(family_setup):
    engine, _, member, _ = family_setup
    st = _stock(engine, member.member_id)
    with pytest.raises(ValueError, match="cannot sell"):
        engine.record_stock_transaction(asset_id=st.asset_id, txn_date=date(2025, 2, 1),
                                        txn_type="SELL", quantity=Decimal("11"), price=Decimal("1600"))
    assert len(engine.list_stocks(member.member_id)[0].transactions) == 1
    with pytest.raises(ValueError, match="does not exist"):
        engine.record_stock_price(asset_id=999, price_date=date(2026, 1, 1), price=Decimal("1"))


def test_stock_portfolio_family_and_member_and_in_allocation(family_setup):
    engine, family, member, spouse = family_setup
    _stock(engine, member.member_id, symbol="INFY", qty="10", price="1500")
    _stock(engine, spouse.member_id, symbol="TCS", qty="2", price="4000")

    p = engine.get_stock_portfolio(family.family_id)
    assert {(r.owner_name, r.stock.symbol) for r in p.rows} == {("Raj", "INFY"), ("Asha", "TCS")}
    assert p.total_invested == Decimal("23000.00")
    assert engine.get_stock_portfolio(family.family_id, spouse.member_id).total_invested == Decimal("8000.00")

    a = engine.get_asset_allocation(family.family_id)
    assert {s.category: s.current_value for s in a.slices}["STOCK"] == Decimal("23000.00")


# ----------------------------------------------------------------------
# PF (EPF / PPF)
# ----------------------------------------------------------------------


def _epf(engine, member_id, **kw):
    args = dict(
        member_id=member_id, account_type="EPF", account_number="UAN-1",
        opened_date=date(2018, 7, 1), interest_rate=Decimal("8.25"),
        employee_contribution=Decimal("300000"), employer_contribution=Decimal("250000"),
        balance=Decimal("720000"), balance_date=date(2026, 3, 31),
    )
    args.update(kw)
    return engine.create_pf_account(**args)


def test_epf_account_contributions_balance_and_interest(family_setup):
    engine, _, member, _ = family_setup
    a = _epf(engine, member.member_id)
    assert a.name == "EPF UAN-1" and a.institution == "EPFO"
    assert a.total_contributions == Decimal("550000")
    assert a.balance == Decimal("720000") and a.balance_date == date(2026, 3, 31)
    assert a.interest_earned == Decimal("170000")
    assert a.maturity_date is None


def test_ppf_maturity_and_no_employer_contribution(family_setup):
    engine, _, member, _ = family_setup
    a = engine.create_pf_account(
        member_id=member.member_id, account_type="PPF", account_number="PPF-9",
        opened_date=date(2015, 6, 10), interest_rate=Decimal("7.1"),
        employee_contribution=Decimal("150000"), balance=Decimal("230000"),
        balance_date=date(2026, 4, 1), institution="SBI",
    )
    assert a.maturity_date == date(2031, 4, 1)  # FY 2015-16 + 15 years
    assert a.employer_contribution is None
    with pytest.raises(ValueError, match="no employer contribution"):
        engine.create_pf_account(
            member_id=member.member_id, account_type="PPF", account_number="PPF-X",
            opened_date=date(2020, 1, 1), interest_rate=7, employee_contribution=1000,
            employer_contribution=500, balance=1100, balance_date=date(2026, 1, 1),
        )


def test_pf_update_replaces_totals_and_newer_balance_wins(family_setup):
    engine, _, member, _ = family_setup
    a = _epf(engine, member.member_id)
    b = engine.update_pf_account(
        asset_id=a.asset_id, balance=Decimal("800000"), balance_date=date(2026, 9, 1),
        employee_contribution=Decimal("330000"), employer_contribution=Decimal("275000"),
    )
    assert b.balance == Decimal("800000") and b.total_contributions == Decimal("605000")
    assert b.interest_rate == Decimal("8.25")  # unchanged when not given
    # same date again = correction, the newer entry wins
    c = engine.update_pf_account(
        asset_id=a.asset_id, balance=Decimal("801000"), balance_date=date(2026, 9, 1),
        employee_contribution=Decimal("330000"), employer_contribution=Decimal("275000"),
    )
    assert c.balance == Decimal("801000")
    with pytest.raises(ValueError, match="does not exist"):
        engine.update_pf_account(asset_id=999, balance=1, balance_date=date(2026, 1, 1),
                                 employee_contribution=1)


def test_pf_portfolio_and_allocation(family_setup):
    engine, family, member, spouse = family_setup
    _epf(engine, member.member_id)
    engine.create_pf_account(
        member_id=spouse.member_id, account_type="PPF", account_number="PPF-1",
        opened_date=date(2020, 5, 1), interest_rate=Decimal("7.1"),
        employee_contribution=Decimal("100000"), balance=Decimal("125000"),
        balance_date=date(2026, 4, 1),
    )
    p = engine.get_pf_portfolio(family.family_id)
    assert p.total_contributions == Decimal("650000")
    assert p.total_balance == Decimal("845000")
    assert p.epf_balance == Decimal("720000") and p.ppf_balance == Decimal("125000")
    assert engine.get_pf_portfolio(family.family_id, spouse.member_id).total_balance == Decimal("125000")

    slices = {s.category: s for s in engine.get_asset_allocation(family.family_id).slices}
    assert slices["EPF"].invested == Decimal("550000") and slices["EPF"].current_value == Decimal("720000")
    assert slices["PPF"].current_value == Decimal("125000")


# ----------------------------------------------------------------------
# Gold
# ----------------------------------------------------------------------


def _gold(engine, member_id, form="PHYSICAL", purity="22K", grams="20", rate="5000", **kw):
    return engine.create_gold(
        member_id=member_id, form=form, purity=purity, quantity_grams=Decimal(grams),
        purchase_rate_per_gram=Decimal(rate), purchase_date=date(2023, 4, 1), **kw,
    )


def test_gold_purchase_invested_and_valued_at_buy_rate(family_setup):
    engine, _, member, _ = family_setup
    g = _gold(engine, member.member_id, name="Necklace")
    assert g.name == "Necklace" and g.purity == "22K"
    assert g.invested_amount == Decimal("100000.00")
    assert g.current_value == Decimal("100000.00")
    assert g.current_rate_per_gram == Decimal("5000")


def test_gold_rate_revalues_every_holding_by_purity(family_setup):
    engine, family, member, spouse = family_setup
    _gold(engine, member.member_id, purity="22K", grams="20", rate="5000")
    _gold(engine, spouse.member_id, form="SGB", purity="24K", grams="10", rate="5500",
          sgb_interest_rate=Decimal("2.5"))

    p = engine.record_gold_rate(family_id=family.family_id, rate_24k_per_gram=Decimal("9600"),
                                as_of=date(2026, 9, 29))

    by_purity = {r.gold.purity: r.gold for r in p.rows}
    assert by_purity["22K"].current_rate_per_gram == Decimal("8800.00")  # 9600 x 22/24
    assert by_purity["22K"].current_value == Decimal("176000.00")
    assert by_purity["24K"].current_value == Decimal("96000.00")
    assert by_purity["24K"].sgb_interest_rate == Decimal("2.5")
    assert p.total_grams == Decimal("30")
    assert p.total_invested == Decimal("155000.00")
    assert p.total_current_value == Decimal("272000.00")
    assert p.latest_rate_24k == Decimal("9600.00") and p.latest_rate_date == date(2026, 9, 29)
    assert p.xirr is not None and p.xirr > 0
    assert engine.get_gold_portfolio(family.family_id, spouse.member_id).total_grams == Decimal("10")
    slices = {s.category: s for s in engine.get_asset_allocation(family.family_id).slices}
    assert slices["GOLD"].current_value == Decimal("272000.00")


def test_gold_rejects_bad_purity_combinations(family_setup):
    engine, family, member, _ = family_setup
    with pytest.raises(ValueError, match="always 24K"):
        _gold(engine, member.member_id, form="SGB", purity="22K")
    with pytest.raises(ValueError, match="purity"):
        _gold(engine, member.member_id, purity="14K")
    with pytest.raises(ValueError, match="positive"):
        engine.record_gold_rate(family_id=family.family_id, rate_24k_per_gram=0, as_of=date(2026, 1, 1))


# ----------------------------------------------------------------------
# NPS
# ----------------------------------------------------------------------


def _nps(engine, member_id, tier="I", **kw):
    args = dict(
        member_id=member_id, pran="110012345678", tier=tier, opened_date=date(2019, 4, 1),
        employee_contribution=Decimal("400000"), employer_contribution=Decimal("200000"),
        equity_pct=Decimal("50"), corporate_debt_pct=Decimal("30"), gov_bond_pct=Decimal("20"),
        balance=Decimal("820000"), balance_date=date(2026, 3, 31),
    )
    args.update(kw)
    return engine.create_nps_account(**args)


def test_nps_account_contributions_balance_and_mix(family_setup):
    engine, _, member, _ = family_setup
    a = _nps(engine, member.member_id)
    assert a.name == "NPS Tier I 110012345678"
    assert a.total_contributions == Decimal("600000")
    assert a.gain_loss == Decimal("220000")
    assert (a.equity_pct, a.corporate_debt_pct, a.gov_bond_pct) == (Decimal("50"), Decimal("30"), Decimal("20"))


def test_nps_rejects_bad_tier_and_mix(family_setup):
    engine, _, member, _ = family_setup
    with pytest.raises(ValueError, match="tier"):
        _nps(engine, member.member_id, tier="III")
    with pytest.raises(ValueError, match="add up to 100"):
        _nps(engine, member.member_id, equity_pct=Decimal("60"))


def test_nps_update_keeps_mix_when_omitted_and_portfolio_totals(family_setup):
    engine, family, member, spouse = family_setup
    a = _nps(engine, member.member_id)
    b = engine.update_nps_account(
        asset_id=a.asset_id, balance=Decimal("900000"), balance_date=date(2026, 9, 1),
        employee_contribution=Decimal("420000"), employer_contribution=Decimal("210000"),
    )
    assert b.balance == Decimal("900000") and b.total_contributions == Decimal("630000")
    assert b.equity_pct == Decimal("50")
    _nps(engine, spouse.member_id, tier="II", pran="110099999999", employer_contribution=None,
         employee_contribution=Decimal("50000"), balance=Decimal("56000"),
         equity_pct=0, corporate_debt_pct=0, gov_bond_pct=0)

    p = engine.get_nps_portfolio(family.family_id)
    assert p.total_balance == Decimal("956000")
    assert p.tier1_balance == Decimal("900000") and p.tier2_balance == Decimal("56000")
    assert engine.get_nps_portfolio(family.family_id, spouse.member_id).total_contributions == Decimal("50000")
    slices = {s.category: s for s in engine.get_asset_allocation(family.family_id).slices}
    assert slices["NPS"].current_value == Decimal("956000")
    with pytest.raises(ValueError, match="does not exist"):
        engine.update_nps_account(asset_id=999, balance=1, balance_date=date(2026, 1, 1),
                                  employee_contribution=1)


def test_fd_payout_interest_per_quarter_or_month(family_setup):
    engine, family, member, spouse = family_setup
    engine.create_fd(member_id=member.member_id, bank_name="ICICI", fd_number="Q-1",
                     principal=Decimal("2500000"), interest_rate=Decimal("7.96"),
                     issue_date=date(2025, 3, 14), maturity_date=date(2030, 9, 13), payout_type="QUARTERLY")
    engine.create_fd(member_id=spouse.member_id, bank_name="SBI", fd_number="M-1",
                     principal=Decimal("1200000"), interest_rate=Decimal("7"),
                     issue_date=date(2025, 1, 1), maturity_date=date(2029, 1, 1), payout_type="MONTHLY")
    engine.create_fd(member_id=spouse.member_id, bank_name="SBI", fd_number="C-1",
                     principal=Decimal("500000"), interest_rate=Decimal("7.5"),
                     issue_date=date(2025, 1, 1), maturity_date=date(2029, 1, 1), payout_type="MATURITY")
    engine.create_fd(member_id=member.member_id, bank_name="HDFC", fd_number="OLD-Q",
                     principal=Decimal("100000"), interest_rate=Decimal("7"),
                     issue_date=date(2020, 1, 1), maturity_date=date(2021, 1, 1), payout_type="QUARTERLY")

    p = engine.get_fd_portfolio(family.family_id)
    by_no = {r.fd.fd_number: r.fd for r in p.rows}
    assert by_no["Q-1"].payout_interest == Decimal("49750.00")
    assert by_no["M-1"].payout_interest == Decimal("7000.00")
    assert by_no["C-1"].payout_interest is None  # cumulative: paid at maturity


# ----------------------------------------------------------------------
# Surrender value estimate
# ----------------------------------------------------------------------


def test_surrender_estimate_defaults_from_the_policy_and_saves_nothing(family_setup):
    engine, _, member, _ = family_setup
    pol = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="ENDOWMENT", insurer="LIC", policy_number="E-1",
        premium_amount=Decimal("50000"), premium_frequency="ANNUAL", start_date=date(2016, 1, 1),
        maturity_date=date(2036, 1, 1), sum_assured=Decimal("1000000"),
        accrued_bonus=Decimal("400000"),
    )
    est = engine.estimate_surrender_value(pol.policy_id, as_of=date(2026, 1, 1))
    assert est.method == "TRADITIONAL"
    assert est.installments_payable == 20            # PPT defaults to the 20-year term
    assert est.installments_paid == 11               # due so far on 2026-01-01 (2016..2026)
    assert est.paid_up_value == Decimal("950000.00")  # 10L x 11/20 + 4L bonus
    assert est.estimate == max(est.gsv, est.ssv)
    assert engine.list_insurance_policies(member.member_id)[0].current_surrender_value is None

    over = engine.estimate_surrender_value(pol.policy_id, as_of=date(2026, 1, 1),
                                           premium_paying_term_years=10, installments_paid=10,
                                           ssv_factor="0.5")
    assert over.installments_payable == 10 and over.paid_up_value == Decimal("1400000.00")
    assert over.ssv == Decimal("700000.00")


def test_surrender_estimate_ulip_and_protection_policies(family_setup):
    engine, _, member, _ = family_setup
    ulip = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="ULIP", insurer="HDFC Life", policy_number="U-1",
        premium_amount=Decimal("100000"), premium_frequency="ANNUAL", start_date=date(2024, 1, 1),
    )
    est = engine.estimate_surrender_value(ulip.policy_id, as_of=date(2026, 6, 1), fund_value="250000")
    assert est.method == "ULIP" and est.discontinuance_charge == Decimal("3000.00")
    with pytest.raises(ValueError, match="fund value"):
        engine.estimate_surrender_value(ulip.policy_id)
    term = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC", policy_number="T-1",
        premium_amount=Decimal("12000"), premium_frequency="ANNUAL", start_date=date(2020, 1, 1),
    )
    with pytest.raises(ValueError, match="no surrender value"):
        engine.estimate_surrender_value(term.policy_id)


# ----------------------------------------------------------------------
# FD edit / delete and interest figures
# ----------------------------------------------------------------------


def test_fd_interest_figures_in_summary_and_portfolio(family_setup):
    engine, family, member, _ = family_setup
    engine.create_fd(member_id=member.member_id, bank_name="ICICI Bank", fd_number="4210029821",
                     principal=Decimal("2500000"), interest_rate=Decimal("7.96"),
                     issue_date=date(2025, 3, 14), maturity_date=date(2026, 9, 13),
                     payout_type="QUARTERLY", maturity_value=Decimal("2500000"))
    p = engine.get_fd_portfolio(family.family_id)
    fd = p.rows[0].fd
    assert fd.total_interest_by_maturity == Decimal("298772.60")
    assert fd.total_interest_pct == Decimal("11.95")
    assert fd.current_value == Decimal("2500000.00")  # interest paid out, not in value
    assert p.total_interest_by_maturity == Decimal("298772.60")
    assert p.total_interest_earned_so_far == fd.interest_earned_so_far


def test_update_fd_corrects_terms_and_owner(family_setup):
    engine, family, member, spouse = family_setup
    fd = engine.create_fd(member_id=member.member_id, bank_name="ICIC", fd_number="42100",
                          principal=Decimal("250000"), interest_rate=Decimal("7.96"),
                          issue_date=date(2025, 3, 14), maturity_date=date(2027, 9, 13),
                          payout_type="QUARTERLY")
    fixed = engine.update_fd(asset_id=fd.asset_id, member_id=spouse.member_id, bank_name="ICICI Bank",
                             fd_number="4210029821", principal=Decimal("2500000"),
                             interest_rate=Decimal("7.96"), issue_date=date(2025, 3, 14),
                             maturity_date=date(2027, 9, 13), payout_type="MONTHLY")
    assert fixed.member_id == spouse.member_id
    assert fixed.name == "ICICI Bank FD 4210029821"  # default name regenerated
    assert fixed.principal == Decimal("2500000") and fixed.payout_type == "MONTHLY"
    assert engine.list_fds(member.member_id) == []
    assert [f.fd_number for f in engine.list_fds(spouse.member_id)] == ["4210029821"]
    assert engine.get_fd_portfolio(family.family_id).total_principal == Decimal("2500000")
    assert AssetRepository(engine._db).list_ownership(fd.asset_id) == [(spouse.member_id, Decimal("100"))]
    with pytest.raises(Exception):  # schema: maturity must be after issue — nothing changes
        engine.update_fd(asset_id=fd.asset_id, member_id=spouse.member_id, bank_name="X",
                         fd_number="1", principal=1, interest_rate=1, issue_date=date(2025, 1, 1),
                         maturity_date=date(2024, 1, 1), payout_type="MATURITY")
    assert engine.list_fds(spouse.member_id)[0].bank_name == "ICICI Bank"


def test_delete_fd(family_setup):
    engine, family, member, _ = family_setup
    fd = _fd(engine, member.member_id, "DUP", "100000", date(2030, 1, 1))
    engine.delete_fd(fd.asset_id)
    assert engine.list_fds(member.member_id) == []
    with pytest.raises(ValueError, match="does not exist"):
        engine.delete_fd(fd.asset_id)


# ----------------------------------------------------------------------
# Monthly cash flow
# ----------------------------------------------------------------------


def _cash_flow_family(engine, member, spouse):
    # Spouse: only income is FD interest, and runs a SIP from it.
    engine.create_fd(member_id=spouse.member_id, bank_name="ICICI Bank", fd_number="Q-1",
                     principal=Decimal("2500000"), interest_rate=Decimal("7.96"),
                     issue_date=date(2025, 3, 14), maturity_date=date(2027, 9, 13),
                     payout_type="QUARTERLY")
    mf = _create_mf(engine, spouse.member_id)
    engine.update_mf_sip(asset_id=mf.asset_id, sip_amount=Decimal("10000"), sip_date=5)
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="INCOME", name="Salary",
                              amount=Decimal("150000"))
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="Rent",
                              amount=Decimal("30000"))
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="Car insurance",
                              amount=Decimal("24000"), frequency="ANNUAL")
    return engine.create_insurance_policy(
        member_id=member.member_id, policy_type="ENDOWMENT", insurer="LIC", policy_number="E-1",
        premium_amount=Decimal("86454"), premium_frequency="ANNUAL", start_date=date(2016, 6, 13),
        maturity_date=date(2041, 6, 13),
    )


def test_monthly_cash_flow_lines_and_totals(family_setup):
    engine, family, member, spouse = family_setup
    _cash_flow_family(engine, member, spouse)

    cf = engine.get_monthly_cash_flow(family.family_id, 2026, 10)

    fd_line = [l for l in cf.income if l.source == "FD_INTEREST"][0]
    assert fd_line.amount == Decimal("49750.00") and fd_line.monthly_amount == Decimal("16583.33")
    sip = [l for l in cf.investments if l.source == "SIP"][0]
    assert sip.monthly_amount == Decimal("10000") and sip.owner_name == "Asha"
    car = [l for l in cf.expenses if l.label == "Car insurance"][0]
    assert car.monthly_amount == Decimal("2000.00")  # 24000 a year / 12
    assert cf.total_income == Decimal("166583.33")
    assert cf.total_expenses == Decimal("32000.00")
    assert cf.total_investments == Decimal("10000")
    assert cf.surplus == Decimal("124583.33")
    assert not any(l.source == "PREMIUM" for l in cf.investments)  # June premium, not October

    asha = engine.get_monthly_cash_flow(family.family_id, 2026, 10, spouse.member_id)
    assert asha.surplus == Decimal("6583.33")  # FD interest 16583.33 - SIP 10000


def test_premium_shows_in_its_month_and_counts_only_when_paid(family_setup):
    engine, family, member, spouse = family_setup
    policy = _cash_flow_family(engine, member, spouse)

    june = engine.get_monthly_cash_flow(family.family_id, 2026, 6)
    premium = [l for l in june.investments if l.source == "PREMIUM"][0]
    assert premium.due_date == date(2026, 6, 13) and premium.paid is False and not premium.counted
    assert june.total_investments == Decimal("10000")

    engine.set_premium_paid(policy_id=policy.policy_id, due_date=date(2026, 6, 13), paid=True)
    june = engine.get_monthly_cash_flow(family.family_id, 2026, 6)
    assert june.total_investments == Decimal("96454")  # SIP 10000 + premium 86454
    engine.set_premium_paid(policy_id=policy.policy_id, due_date=date(2026, 6, 13), paid=False)
    assert engine.get_monthly_cash_flow(family.family_id, 2026, 6).total_investments == Decimal("10000")

    with pytest.raises(ValueError, match="no premium"):
        engine.set_premium_paid(policy_id=policy.policy_id, due_date=date(2026, 7, 13), paid=True)


def test_monthly_premium_appears_every_month_and_health_is_an_expense(family_setup):
    engine, family, member, _ = family_setup
    engine.create_insurance_policy(
        member_id=member.member_id, policy_type="HEALTH", insurer="Star", policy_number="H-1",
        premium_amount=Decimal("1500"), premium_frequency="MONTHLY", start_date=date(2025, 1, 31),
    )
    for month in (2, 3, 11):
        cf = engine.get_monthly_cash_flow(family.family_id, 2026, month)
        assert [l.source for l in cf.expenses] == ["PREMIUM"]
    assert engine.get_monthly_cash_flow(family.family_id, 2026, 2).expenses[0].due_date == date(2026, 2, 28)


def test_cash_flow_items_update_delete_and_validation(family_setup):
    engine, family, member, spouse = family_setup
    item = engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE",
                                     name="Maid", amount=Decimal("5000"))
    moved = engine.update_cash_flow_item(item_id=item.item_id, member_id=spouse.member_id,
                                         flow_type="EXPENSE", name="Cook", amount=Decimal("6000"))
    assert moved.member_id == spouse.member_id and moved.name == "Cook"
    engine.delete_cash_flow_item(item.item_id)
    assert engine.get_monthly_cash_flow(family.family_id, 2026, 10).expenses == ()
    with pytest.raises(ValueError, match="does not exist"):
        engine.delete_cash_flow_item(item.item_id)
    with pytest.raises(ValueError, match="positive"):
        engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="x", amount=0)
    with pytest.raises(ValueError, match="type"):
        engine.add_cash_flow_item(member_id=member.member_id, flow_type="GIFT", name="x", amount=1)
    mf = _create_mf(engine, member.member_id)
    with pytest.raises(ValueError, match="SIP amount"):
        engine.update_mf_sip(asset_id=mf.asset_id, sip_amount=Decimal("-5"))


# ----------------------------------------------------------------------
# Bank accounts and cash in bank (migration 006); MF SIP / lumpsum split
# ----------------------------------------------------------------------


def test_first_bank_account_becomes_default_and_default_can_move(family_setup):
    engine, _, member, _ = family_setup
    icici = engine.add_bank_account(member_id=member.member_id, bank_name="ICICI Bank",
                                    balance=Decimal("1000"), balance_as_of=date(2026, 1, 1))
    hdfc = engine.add_bank_account(member_id=member.member_id, bank_name="HDFC Bank",
                                   account_last4="1234", balance=Decimal("500"),
                                   balance_as_of=date(2026, 1, 1))
    assert icici.is_default and not hdfc.is_default
    engine.update_bank_account(account_id=hdfc.account_id, bank_name="HDFC Bank",
                               account_last4="1234", make_default=True)
    accounts = {a.bank_name: a for a in engine.list_family_bank_accounts(1)}
    assert accounts["HDFC Bank"].is_default and not accounts["ICICI Bank"].is_default
    engine.close_bank_account(hdfc.account_id)
    left = engine.list_family_bank_accounts(1)
    assert [a.bank_name for a in left] == ["ICICI Bank"] and left[0].is_default
    with pytest.raises(ValueError, match="last 4 digits"):
        engine.add_bank_account(member_id=member.member_id, bank_name="X", account_last4="12345",
                                balance=0, balance_as_of=date(2026, 1, 1))


def test_estimated_cash_in_bank_from_rows_since_the_balance_date(family_setup):
    engine, family, member, _ = family_setup
    main = engine.add_bank_account(member_id=member.member_id, bank_name="ICICI Bank",
                                   balance=Decimal("100000"), balance_as_of=date(2026, 1, 15))
    card = engine.add_bank_account(member_id=member.member_id, bank_name="HDFC Bank",
                                   balance=Decimal("5000"), balance_as_of=date(2026, 1, 15))
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="INCOME", category="Salary",
                              name="", amount=Decimal("150000"), payment_mode="BANK_TRANSFER")
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", category="Rent",
                              name="", amount=Decimal("30000"), payment_mode="UPI")
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", category="Maid",
                              name="", amount=Decimal("5000"), payment_mode="CASH")
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", category="Grocery",
                              name="", amount=Decimal("2000"), payment_mode="CARD",
                              account_id=card.account_id)
    mf = _create_mf(engine, member.member_id)
    engine.update_mf_sip(asset_id=mf.asset_id, sip_amount=Decimal("10000"))
    policy = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="HEALTH", insurer="Star", policy_number="H-1",
        premium_amount=Decimal("12000"), premium_frequency="ANNUAL", start_date=date(2025, 3, 1),
    )
    engine.set_premium_paid(policy_id=policy.policy_id, due_date=date(2026, 3, 1), paid=True)

    cf = engine.get_monthly_cash_flow(family.family_id, 2026, 4, today=date(2026, 4, 20))

    by_bank = {a.bank_name: a for a in cf.accounts}
    # 3 whole months (Feb, Mar, Apr); main: +150000 -30000 -10000 SIP = +110000 a month,
    # minus the 12000 premium ticked paid; cash maid never touches a bank.
    assert by_bank["ICICI Bank"].months_elapsed == 3
    assert by_bank["ICICI Bank"].monthly_net == Decimal("110000")
    assert by_bank["ICICI Bank"].estimated_balance == Decimal("418000")
    assert by_bank["HDFC Bank"].estimated_balance == Decimal("-1000")  # 5000 - 3 x 2000 grocery
    assert cf.total_cash_in_bank == Decimal("417000")
    rent = [l for l in cf.expenses if l.label == "Rent"][0]
    assert rent.category == "Rent" and rent.account_label == "ICICI Bank"
    maid = [l for l in cf.expenses if l.label == "Maid"][0]
    assert maid.payment_mode == "CASH" and maid.account_id is None
    assert cf.unassigned_monthly_net == 0

    engine.set_bank_balance(account_id=main.account_id, balance=Decimal("420000"), as_of=date(2026, 4, 20))
    cf = engine.get_monthly_cash_flow(family.family_id, 2026, 4, today=date(2026, 4, 20))
    assert {a.bank_name: a for a in cf.accounts}["ICICI Bank"].estimated_balance == Decimal("420000")


def test_rows_without_any_account_are_reported_unassigned(family_setup):
    engine, family, member, _ = family_setup
    engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="Rent",
                              amount=Decimal("30000"), payment_mode="UPI")
    cf = engine.get_monthly_cash_flow(family.family_id, 2026, 4, today=date(2026, 4, 20))
    assert cf.accounts == () and cf.unassigned_monthly_net == Decimal("-30000")


def test_cash_flow_row_account_must_be_the_members_own(family_setup):
    engine, _, member, spouse = family_setup
    theirs = engine.add_bank_account(member_id=spouse.member_id, bank_name="SBI",
                                     balance=0, balance_as_of=date(2026, 1, 1))
    with pytest.raises(ValueError, match="own bank accounts"):
        engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="Rent",
                                  amount=1, payment_mode="UPI", account_id=theirs.account_id)
    cash = engine.add_cash_flow_item(member_id=spouse.member_id, flow_type="EXPENSE", name="Maid",
                                     amount=1, payment_mode="CASH", account_id=theirs.account_id)
    assert cash.account_id is None  # cash never names a bank account
    with pytest.raises(ValueError, match="paid by"):
        engine.add_cash_flow_item(member_id=member.member_id, flow_type="EXPENSE", name="x",
                                  amount=1, payment_mode="CHEQUE")


def test_mf_sip_and_lumpsum_split_and_type(family_setup):
    engine, family, member, _ = family_setup
    mf = _create_mf(engine, member.member_id)  # first purchase is a SIP instalment
    assert engine.list_mfs(member.member_id)[0].investment_type == "SIP"
    engine.record_mf_transaction(asset_id=mf.asset_id, txn_date=date(2025, 6, 1), txn_type="LUMPSUM",
                                 units=Decimal("50"), nav=Decimal("100"), amount=Decimal("5000"))
    fund = engine.list_mfs(member.member_id)[0]
    assert fund.investment_type == "SIP + LUMPSUM"
    assert fund.lumpsum_invested == Decimal("5000")
    assert fund.sip_invested + fund.lumpsum_invested == fund.invested_amount
    p = engine.get_mf_portfolio(family.family_id)
    assert p.total_lumpsum_invested == Decimal("5000")
    assert p.total_sip_invested == fund.sip_invested


# ----------------------------------------------------------------------
# Stocks: one holding per person and broker, grouped rows, snapshot
# ----------------------------------------------------------------------


def _buy(engine, member_id, symbol, qty, price, broker="Zerodha", day=date(2025, 1, 10)):
    return engine.create_stock(
        member_id=member_id, broker=broker, symbol=symbol, exchange="NSE",
        initial_txn_date=day, initial_quantity=Decimal(qty), initial_price=Decimal(price),
    )


def test_buying_a_held_stock_again_adds_to_the_same_holding(family_setup):
    engine, _, member, _ = family_setup
    first = _buy(engine, member.member_id, "HINDPETRO", "100", "300", broker="HDFC Security")
    again = _buy(engine, member.member_id, "hindpetro", "50", "360", broker=" hdfc security ",
                 day=date(2025, 6, 1))
    assert again.asset_id == first.asset_id
    stocks = engine.list_stocks(member.member_id)
    assert len(stocks) == 1
    assert stocks[0].quantity == Decimal("150")
    assert stocks[0].avg_buy_price == Decimal("320.0000")  # (30000 + 18000) / 150


def test_existing_duplicates_are_merged_once(family_setup):
    engine, family, member, _ = family_setup
    assets, txns = AssetRepository(engine._db), engine._stock_txns
    ids = []
    for qty, price, day in (("10", "300", date(2025, 1, 1)), ("20", "330", date(2025, 3, 1)),
                            ("10", "360", date(2025, 5, 1))):
        asset, _ = assets.create_stock(member.member_id, "HINDPETRO", "HDFC Security", "HINDPETRO",
                                       "NSE", 0, day, [(member.member_id, Decimal("100"))],
                                       broker="HDFC Security")
        txns.create_transaction(asset.asset_id, day, "BUY", Decimal(qty), Decimal(price))
        ids.append(asset.asset_id)
    engine.record_stock_price(asset_id=ids[-1], price_date=date(2026, 9, 29), price=Decimal("400"))
    assert len(engine.list_stocks(member.member_id)) == 3

    merged = engine.merge_duplicate_stock_holdings()

    assert merged == ["Raj: 3 HINDPETRO holdings merged into one"]
    [stock] = engine.list_stocks(member.member_id)
    assert stock.asset_id == ids[0]
    assert stock.quantity == Decimal("40")
    assert stock.avg_buy_price == Decimal("330.0000")  # (3000 + 6600 + 3600) / 40
    assert stock.current_price == Decimal("400")       # newest price kept
    assert stock.current_value == Decimal("16000.00")
    assert engine.merge_duplicate_stock_holdings() == []  # nothing left to merge
    assert engine.get_asset_allocation(family.family_id).total_invested == Decimal("13200.00")


def test_groups_combine_brokers_and_members(family_setup):
    engine, family, member, spouse = family_setup
    _buy(engine, member.member_id, "INFY", "10", "1500", broker="Zerodha")
    _buy(engine, member.member_id, "INFY", "10", "1300", broker="Groww")
    _buy(engine, spouse.member_id, "INFY", "20", "1400", broker="Zerodha")
    _buy(engine, spouse.member_id, "TCS", "5", "4000")

    p = engine.get_stock_portfolio(family.family_id)
    infy = [g for g in p.groups if g.symbol == "INFY"][0]
    assert infy.quantity == Decimal("40") and len(infy.parts) == 3
    assert infy.avg_buy_price == Decimal("1400.0000")  # (15000 + 13000 + 28000) / 40
    assert [g.symbol for g in p.groups] == ["INFY", "TCS"]  # by value, largest first
    assert sum(g.weight_pct for g in p.groups) == Decimal("100.00")

    own = engine.get_stock_portfolio(family.family_id, member.member_id)
    assert [len(g.parts) for g in own.groups] == [2]


def test_snapshot_sector_cap_concentration_and_risk_check(family_setup):
    engine, family, member, _ = family_setup
    engine.update_stock_classification_from_nse([
        ("INFY", "Infosys Ltd.", "Information Technology", "LARGE"),
        ("TCS", "Tata Consultancy Services Ltd.", "Information Technology", "LARGE"),
        ("HINDPETRO", "Hindustan Petroleum Corporation Ltd.", "Oil Gas & Consumable Fuels", "LARGE"),
    ], date(2026, 9, 1))
    _buy(engine, member.member_id, "INFY", "10", "1000")        # 10,000
    _buy(engine, member.member_id, "TCS", "5", "2000")          # 10,000
    _buy(engine, member.member_id, "TINYCO", "40", "500")       # 20,000 - not in Nifty 500
    engine.set_member_risk_tolerance(member.member_id, "CONSERVATIVE")

    a = engine.get_stock_portfolio(family.family_id).analysis

    sectors = {sl.name: sl.pct for sl in a.sector_split}
    assert sectors == {"Unclassified": Decimal("50.00"), "Information Technology": Decimal("50.00")}
    caps = {sl.name: sl.pct for sl in a.cap_split}
    assert caps == {"SMALL": Decimal("50.00"), "LARGE": Decimal("50.00")}  # outside Nifty 500 = small
    assert a.sector_warnings == ("Information Technology is 50.00% of the portfolio (above 25%)",)
    assert a.stock_count == 3 and a.largest_symbol == "TINYCO" and a.largest_pct == Decimal("50.00")
    assert a.effective_stocks == Decimal("2.7")  # 1 / (0.25 + 0.0625 + 0.0625)... = 2.67
    assert a.diversification == "LOW"
    assert a.classification_date == date(2026, 9, 1)
    [rc] = a.risk_checks
    assert rc.portfolio_risk == "VERY_HIGH"  # 50% small (+2), largest 50% (+1), 3 stocks (+1)
    assert rc.fits is False and rc.risk_tolerance == "CONSERVATIVE"
    assert rc.stock_share_of_wealth_pct == Decimal("100.00")
    assert any("small caps" in r for r in rc.reasons)

    engine.set_stock_classification(symbol="tinyco", sector="Chemicals", cap_category="MID")
    engine.update_stock_classification_from_nse([("TINYCO", "x", "Other", "SMALL")], date(2026, 10, 1))
    a = engine.get_stock_portfolio(family.family_id).analysis
    assert {sl.name for sl in a.sector_split} == {"Chemicals", "Information Technology"}  # manual kept
    with pytest.raises(ValueError, match="risk tolerance"):
        engine.set_member_risk_tolerance(member.member_id, "YOLO")


def test_snapshot_before_any_classification_says_unknown(family_setup):
    engine, family, member, _ = family_setup
    _buy(engine, member.member_id, "INFY", "10", "1000")
    a = engine.get_stock_portfolio(family.family_id).analysis
    assert [sl.name for sl in a.cap_split] == ["UNKNOWN"]
    assert a.risk_checks[0].fits is None  # no tolerance picked


def test_update_insurance_policy_corrects_terms_owner_and_status(family_setup):
    engine, family, member, spouse = family_setup
    pol = engine.create_insurance_policy(
        member_id=member.member_id, policy_type="TERM", insurer="LIC", policy_number="P-1",
        premium_amount=Decimal("1200"), premium_frequency="ANNUAL", start_date=date(2020, 1, 1),
        current_surrender_value=Decimal("100"), accrued_bonus=Decimal("50"),
    )
    fixed = engine.update_insurance_policy(
        policy_id=pol.policy_id, member_id=spouse.member_id, policy_type="ENDOWMENT",
        insurer="LIC India", policy_number="997385864", premium_amount=Decimal("86454"),
        premium_frequency="ANNUAL", start_date=date(2016, 6, 13), sum_assured=Decimal("2000000"),
        maturity_date=date(2041, 6, 13),
    )
    assert fixed.member_id == spouse.member_id and fixed.policy_type == "ENDOWMENT"
    assert fixed.premium_amount == Decimal("86454") and fixed.maturity_date == date(2041, 6, 13)
    assert fixed.current_surrender_value == Decimal("100") and fixed.accrued_bonus == Decimal("50")  # kept
    assert engine.list_insurance_policies(member.member_id) == []

    matured = engine.update_insurance_policy(
        policy_id=pol.policy_id, member_id=spouse.member_id, policy_type="ENDOWMENT",
        insurer="LIC India", policy_number="997385864", premium_amount=Decimal("86454"),
        premium_frequency="ANNUAL", start_date=date(2016, 6, 13), status="MATURED",
    )
    assert matured.status == "MATURED" and matured.next_due_date is None
    assert engine.get_insurance_portfolio(family.family_id).total_annual_premium == Decimal("0")
    with pytest.raises(ValueError, match="does not exist"):
        engine.update_insurance_policy(
            policy_id=999, member_id=member.member_id, policy_type="TERM", insurer="X",
            policy_number="1", premium_amount=1, premium_frequency="ANNUAL", start_date=date(2020, 1, 1),
        )
    with pytest.raises(Exception):  # schema: premium must be positive
        engine.update_insurance_policy(
            policy_id=pol.policy_id, member_id=member.member_id, policy_type="TERM", insurer="X",
            policy_number="1", premium_amount=0, premium_frequency="ANNUAL", start_date=date(2020, 1, 1),
        )


def test_fd_needs_only_the_rate_or_the_maturity_amount(family_setup):
    engine, _, member, _ = family_setup
    by_amount = engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="C-1", principal=Decimal("100000"),
        interest_rate=None, issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
        payout_type="MATURITY", maturity_value=Decimal("107185.90"),
    )
    assert by_amount.interest_rate == Decimal("7.00")
    by_rate = engine.create_fd(
        member_id=member.member_id, bank_name="HDFC", fd_number="C-2", principal=Decimal("100000"),
        interest_rate=Decimal("7"), issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
        payout_type="MATURITY",
    )
    assert by_rate.total_interest_by_maturity == Decimal("7185.90")
    with pytest.raises(ValueError, match="either the interest rate or the maturity amount"):
        engine.create_fd(
            member_id=member.member_id, bank_name="HDFC", fd_number="C-3", principal=100000,
            interest_rate=None, issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
            payout_type="MATURITY",
        )
    with pytest.raises(ValueError, match="enter the interest rate"):
        engine.create_fd(
            member_id=member.member_id, bank_name="HDFC", fd_number="Q-1", principal=100000,
            interest_rate=None, issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
            payout_type="QUARTERLY", maturity_value=100000,
        )


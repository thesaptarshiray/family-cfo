"""Tests for bank-account persistence and ownership rules."""

from decimal import Decimal

import pytest

from repository.bank_repository import BankAccount, BankRepository
from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def database(tmp_path):
    database = Database(tmp_path / "bank.db")
    initialize_schema(database)
    return database


@pytest.fixture
def bank_repo(database):
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return BankRepository(database), member.member_id


def test_create_bank_account_defaults_current_balance_to_zero(bank_repo):
    repository, member_id = bank_repo

    account = repository.create_account(
        member_id=member_id,
        bank_name="Example Bank",
        account_type="SAVINGS",
        account_number_masked="1234",
        opening_balance=Decimal("12500.00"),
        ownership=[(member_id, Decimal("100"))],
    )

    assert account == BankAccount(
        account_id=1,
        member_id=member_id,
        bank_name="Example Bank",
        account_type="SAVINGS",
        account_number_masked="1234",
        opening_balance=Decimal("12500.00"),
        current_balance=Decimal("0"),
        is_active=True,
    )


def test_get_account_and_ownership(bank_repo):
    repository, member_id = bank_repo
    account = repository.create_account(
        member_id, "Example Bank", "JOINT", "9876", ownership=[(member_id, 100)]
    )

    assert repository.get_account(account.account_id) == account
    assert repository.list_ownership(account.account_id) == [(member_id, Decimal("100"))]


def test_create_joint_account_requires_ownership_sum_of_100(bank_repo):
    repository, member_id = bank_repo

    with pytest.raises(ValueError, match="sum to 100"):
        repository.create_account(
            member_id,
            "Example Bank",
            "JOINT",
            "1234",
            ownership=[(member_id, Decimal("60"))],
        )


def test_create_account_requires_primary_holder_ownership(bank_repo):
    repository, member_id = bank_repo

    with pytest.raises(ValueError, match="primary holder"):
        repository.create_account(
            member_id,
            "Example Bank",
            "SAVINGS",
            "1234",
            ownership=[(999, Decimal("100"))],
        )


def test_failed_account_creation_writes_nothing(bank_repo):
    repository, member_id = bank_repo

    with pytest.raises(ValueError):
        repository.create_account(
            member_id,
            "Example Bank",
            "SAVINGS",
            "1234",
            ownership=[(member_id, Decimal("99"))],
        )

    assert repository.list_accounts(member_id) == []


def test_nonexistent_member_is_rejected(bank_repo):
    repository, _ = bank_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_account(
            999,
            "Example Bank",
            "SAVINGS",
            "1234",
            ownership=[(999, Decimal("100"))],
        )

    assert repository.list_accounts(999) == []

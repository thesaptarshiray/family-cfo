"""Tests for insurance-policy persistence."""

from datetime import date
from decimal import Decimal

import pytest

from repository.database import Database
from repository.errors import RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.insurance_repository import InsurancePolicy, InsurancePolicyRepository
from repository.schema import initialize_schema


@pytest.fixture
def policy_repo(tmp_path):
    database = Database(tmp_path / "insurance.db")
    initialize_schema(database)
    family_repo = FamilyRepository(database)
    family = family_repo.create_family("Sharma")
    member = family_repo.create_family_member(family.family_id, "Raj", "self")
    return InsurancePolicyRepository(database), member.member_id


def test_create_policy_returns_the_stored_row(policy_repo):
    repository, member_id = policy_repo

    policy = repository.create_policy(
        member_id, "TERM", "LIC", "POL-001", Decimal("12000"), "ANNUAL",
        date(2026, 1, 1), sum_assured=Decimal("5000000"),
        maturity_date=date(2056, 1, 1),
    )

    assert policy == InsurancePolicy(
        policy_id=1, member_id=member_id, policy_type="TERM", insurer="LIC",
        policy_number="POL-001", sum_assured=Decimal("5000000"),
        premium_amount=Decimal("12000"), premium_frequency="ANNUAL",
        start_date=date(2026, 1, 1), maturity_date=date(2056, 1, 1),
        current_surrender_value=None, status="ACTIVE",
    )
    assert repository.get_policy(policy.policy_id) == policy


def test_create_policy_with_minimal_fields_leaves_optionals_null(policy_repo):
    repository, member_id = policy_repo

    policy = repository.create_policy(
        member_id, "HEALTH", "Example Insurer", "POL-002", 8000, "MONTHLY",
        date(2026, 3, 1),
    )

    assert policy.sum_assured is None
    assert policy.maturity_date is None
    assert policy.current_surrender_value is None
    assert policy.status == "ACTIVE"


def test_get_policy_missing_returns_none(policy_repo):
    repository, _ = policy_repo
    assert repository.get_policy(999) is None


def test_list_policies_returns_only_that_members_policies_oldest_first(policy_repo):
    repository, member_id = policy_repo
    family_repo = FamilyRepository(repository._db)
    family_id = family_repo.get_family_member(member_id).family_id
    other_member = family_repo.create_family_member(family_id, "Asha", "spouse")

    first = repository.create_policy(
        member_id, "TERM", "LIC", "POL-A", 12000, "ANNUAL", date(2025, 1, 1)
    )
    second = repository.create_policy(
        member_id, "HEALTH", "Example Insurer", "POL-B", 8000, "MONTHLY", date(2026, 1, 1)
    )
    repository.create_policy(
        other_member.member_id, "MOTOR", "Example Insurer", "POL-C", 3000, "ANNUAL",
        date(2026, 1, 1),
    )

    assert repository.list_policies(member_id) == [first, second]


def test_list_policies_for_unknown_member_is_empty(policy_repo):
    repository, _ = policy_repo
    assert repository.list_policies(999) == []


def test_create_policy_rejects_invalid_policy_type(policy_repo):
    repository, member_id = policy_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_policy(
            member_id, "TRAVEL", "Example Insurer", "POL-D", 1000, "ANNUAL",
            date(2026, 1, 1),
        )
    assert repository.list_policies(member_id) == []


def test_create_policy_rejects_non_positive_premium(policy_repo):
    repository, member_id = policy_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_policy(
            member_id, "TERM", "LIC", "POL-E", 0, "ANNUAL", date(2026, 1, 1)
        )
    assert repository.list_policies(member_id) == []


def test_create_policy_for_nonexistent_member_is_rejected(policy_repo):
    repository, _ = policy_repo

    with pytest.raises(RepositoryIntegrityError):
        repository.create_policy(
            999, "TERM", "LIC", "POL-F", 12000, "ANNUAL", date(2026, 1, 1)
        )

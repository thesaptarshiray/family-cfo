"""
Tests for `repository.family_repository.FamilyRepository`.

Each test runs against its own temporary SQLite file initialized from the
approved V6 DDL. No network, no shared state between tests.
"""

from datetime import date

import pytest

from repository.database import Database
from repository.errors import RepositoryError, RepositoryIntegrityError
from repository.family_repository import FamilyRepository
from repository.schema import initialize_schema


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "family.db"


@pytest.fixture
def database(db_path):
    database = Database(db_path)
    initialize_schema(database)
    return database


@pytest.fixture
def repo(database):
    return FamilyRepository(database)


# --- Family -----------------------------------------------------------------


def test_create_family(repo):
    family = repo.create_family("Sharma", base_currency="USD")

    assert family.family_id == 1
    assert family.family_name == "Sharma"
    assert family.base_currency == "USD"


def test_create_family_uses_schema_default_currency(repo):
    family = repo.create_family("Sharma")

    assert family.base_currency == "INR"


def test_get_family(repo):
    created = repo.create_family("Sharma")

    assert repo.get_family(created.family_id) == created


def test_get_family_missing_returns_none(repo):
    assert repo.get_family(999) is None


def test_list_families_is_empty_initially(repo):
    assert repo.list_families() == []


def test_list_families_returns_every_family_oldest_first(repo):
    first = repo.create_family("Sharma")
    second = repo.create_family("Verma")

    assert repo.list_families() == [first, second]


def test_update_family(repo):
    created = repo.create_family("Sharma")

    updated = repo.update_family(created.family_id, family_name="Sharma-Rao")

    assert updated.family_name == "Sharma-Rao"
    assert updated.base_currency == "INR"  # not supplied, so unchanged
    assert repo.get_family(created.family_id) == updated


def test_update_family_missing_returns_none(repo):
    assert repo.update_family(999, family_name="Nobody") is None


def test_update_family_rejects_null_name(repo):
    created = repo.create_family("Sharma")

    with pytest.raises(RepositoryIntegrityError):
        repo.update_family(created.family_id, family_name=None)

    assert repo.get_family(created.family_id).family_name == "Sharma"


# --- Family member ----------------------------------------------------------


def test_create_family_member(repo):
    family = repo.create_family("Sharma")

    member = repo.create_family_member(
        family.family_id, "Asha", "spouse", date_of_birth=date(1990, 5, 17)
    )

    assert member.member_id == 1
    assert member.family_id == family.family_id
    assert member.name == "Asha"
    assert member.relationship == "spouse"
    assert member.date_of_birth == date(1990, 5, 17)
    assert member.is_active is True  # schema default


def test_create_family_member_without_date_of_birth(repo):
    family = repo.create_family("Sharma")

    member = repo.create_family_member(family.family_id, "Asha", "spouse")

    assert member.date_of_birth is None


def test_get_family_member(repo):
    family = repo.create_family("Sharma")
    created = repo.create_family_member(
        family.family_id, "Asha", "spouse", date_of_birth=date(1990, 5, 17)
    )

    assert repo.get_family_member(created.member_id) == created


def test_get_family_member_missing_returns_none(repo):
    assert repo.get_family_member(999) is None


def test_multiple_members_belong_to_same_family(repo):
    family = repo.create_family("Sharma")

    a = repo.create_family_member(family.family_id, "Raj", "self")
    b = repo.create_family_member(family.family_id, "Asha", "spouse")
    c = repo.create_family_member(family.family_id, "Kabir", "child")

    assert len({a.member_id, b.member_id, c.member_id}) == 3
    assert {a.family_id, b.family_id, c.family_id} == {family.family_id}


def test_list_family_members_only_returns_that_familys_members(repo):
    sharma = repo.create_family("Sharma")
    iyer = repo.create_family("Iyer")
    raj = repo.create_family_member(sharma.family_id, "Raj", "self")
    asha = repo.create_family_member(sharma.family_id, "Asha", "spouse")
    repo.create_family_member(iyer.family_id, "Meera", "self")

    assert repo.list_family_members(sharma.family_id) == [raj, asha]


def test_list_family_members_unknown_family_is_empty(repo):
    assert repo.list_family_members(999) == []


def test_list_family_members_active_only(repo):
    family = repo.create_family("Sharma")
    raj = repo.create_family_member(family.family_id, "Raj", "self")
    asha = repo.create_family_member(family.family_id, "Asha", "spouse")
    repo.update_family_member(asha.member_id, is_active=False)

    assert [m.name for m in repo.list_family_members(family.family_id)] == [
        "Raj",
        "Asha",
    ]
    assert repo.list_family_members(family.family_id, active_only=True) == [raj]


def test_update_family_member(repo):
    family = repo.create_family("Sharma")
    created = repo.create_family_member(
        family.family_id, "Asha", "spouse", date_of_birth=date(1990, 5, 17)
    )

    updated = repo.update_family_member(
        created.member_id, name="Asha Sharma", is_active=False
    )

    assert updated.name == "Asha Sharma"
    assert updated.is_active is False
    # fields that were not supplied are untouched
    assert updated.relationship == "spouse"
    assert updated.date_of_birth == date(1990, 5, 17)
    assert updated.family_id == family.family_id
    assert repo.get_family_member(created.member_id) == updated


def test_update_family_member_can_clear_date_of_birth(repo):
    family = repo.create_family("Sharma")
    created = repo.create_family_member(
        family.family_id, "Asha", "spouse", date_of_birth=date(1990, 5, 17)
    )

    updated = repo.update_family_member(created.member_id, date_of_birth=None)

    assert updated.date_of_birth is None


def test_update_family_member_missing_returns_none(repo):
    assert repo.update_family_member(999, name="Nobody") is None


# --- Constraint failures ----------------------------------------------------


def test_create_member_for_nonexistent_family_is_rejected(repo):
    with pytest.raises(RepositoryIntegrityError) as excinfo:
        repo.create_family_member(999, "Ghost", "self")

    assert "FOREIGN KEY" in str(excinfo.value)
    # nothing was written
    assert repo.list_family_members(999) == []
    assert repo.get_family_member(1) is None


def test_null_name_is_rejected_by_schema(repo):
    family = repo.create_family("Sharma")

    with pytest.raises(RepositoryIntegrityError):
        repo.create_family_member(family.family_id, None, "self")

    assert repo.list_family_members(family.family_id) == []


def test_integrity_error_chains_original_sqlite_exception(repo):
    import sqlite3

    with pytest.raises(RepositoryIntegrityError) as excinfo:
        repo.create_family_member(999, "Ghost", "self")

    assert isinstance(excinfo.value.__cause__, sqlite3.IntegrityError)


def test_unexpected_database_error_is_repository_error_not_integrity(db_path):
    # Schema was never applied, so the tables don't exist: an ordinary
    # operational failure, not a constraint violation.
    repo = FamilyRepository(Database(db_path))

    with pytest.raises(RepositoryError) as excinfo:
        repo.create_family("Sharma")

    assert not isinstance(excinfo.value, RepositoryIntegrityError)


# --- Parameterization -------------------------------------------------------


def test_values_are_stored_verbatim_not_interpreted_as_sql(repo):
    hostile = "Robert'); DROP TABLE family_member;--"
    family = repo.create_family(hostile)
    member = repo.create_family_member(family.family_id, hostile, hostile)

    assert repo.get_family(family.family_id).family_name == hostile
    assert repo.get_family_member(member.member_id).name == hostile
    # the table still exists and still works
    assert repo.list_family_members(family.family_id) == [member]


# --- Persistence ------------------------------------------------------------


def test_reopening_database_preserves_data(db_path, repo):
    family = repo.create_family("Sharma", base_currency="USD")
    member = repo.create_family_member(
        family.family_id, "Asha", "spouse", date_of_birth=date(1990, 5, 17)
    )
    repo.update_family_member(member.member_id, name="Asha Sharma")

    # Simulate an application restart: brand-new Database and repository
    # objects over the same file.
    reopened = FamilyRepository(Database(db_path))

    assert reopened.get_family(family.family_id) == family
    assert reopened.list_family_members(family.family_id) == [
        reopened.get_family_member(member.member_id)
    ]
    assert reopened.get_family_member(member.member_id).name == "Asha Sharma"
    assert reopened.get_family_member(member.member_id).date_of_birth == date(
        1990, 5, 17
    )

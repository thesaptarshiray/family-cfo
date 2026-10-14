"""
Family and family-member persistence (Block 2A).

`FamilyRepository` turns method calls into parameterized SQL against the
`family` and `family_member` tables of the approved V6 schema, and turns the
resulting rows into plain dataclasses. That is all it does:

  - no financial calculation and no business-rule validation — the only
    rules enforced here are the ones the schema itself declares (NOT NULL,
    CHECK, FOREIGN KEY), and SQLite enforces those, not this module;
  - every value reaches SQL as a bound parameter, never through string
    formatting; the only text spliced into a statement is fixed literals
    written in this file;
  - every write goes through `Database.transaction()`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Any, List, Optional

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

# Marks "argument not supplied" in update methods, so that passing `None`
# can still mean "set the column to NULL" (date_of_birth is nullable).
_UNSET: Any = object()

_SELECT_FAMILY = (
    "SELECT family_id, family_name, base_currency "
    "FROM family WHERE family_id = ?"
)
_SELECT_MEMBER = (
    "SELECT member_id, family_id, name, relationship, date_of_birth, is_active, "
    "risk_tolerance, life_stage, heir_member_id, retirement_age FROM family_member WHERE member_id = ?"
)


@dataclass(frozen=True)
class Family:
    """One row of the `family` table."""

    family_id: int
    family_name: str
    base_currency: str


@dataclass(frozen=True)
class FamilyMember:
    """One row of the `family_member` table."""

    member_id: int
    family_id: int
    name: str
    relationship: str
    date_of_birth: Optional[date]
    is_active: bool
    risk_tolerance: Optional[str] = None  # migration 007: CONSERVATIVE / MODERATE / AGGRESSIVE
    # migration 011: EARNING / RETIRED / DEPENDENT; who inherits; when an earner retires
    life_stage: Optional[str] = None
    heir_member_id: Optional[int] = None
    retirement_age: Optional[int] = None


def _family_from_row(row: sqlite3.Row) -> Family:
    return Family(
        family_id=row["family_id"],
        family_name=row["family_name"],
        base_currency=row["base_currency"],
    )


def _member_from_row(row: sqlite3.Row) -> FamilyMember:
    dob = row["date_of_birth"]
    return FamilyMember(
        member_id=row["member_id"],
        family_id=row["family_id"],
        name=row["name"],
        relationship=row["relationship"],
        date_of_birth=None if dob is None else date.fromisoformat(dob),
        is_active=bool(row["is_active"]),
        risk_tolerance=row["risk_tolerance"],
        life_stage=row["life_stage"],
        heir_member_id=row["heir_member_id"],
        retirement_age=row["retirement_age"],
    )


def _date_to_db(value: Optional[date]) -> Optional[str]:
    # DATE columns hold ISO-8601 text ("YYYY-MM-DD"); converted explicitly
    # rather than relying on sqlite3's date adapters.
    return None if value is None else value.isoformat()


def _bool_to_db(value: Optional[bool]) -> Optional[int]:
    # `None` is passed through so the NOT NULL constraint, not this module,
    # decides whether it is acceptable.
    return None if value is None else int(value)


class FamilyRepository(BaseRepository):
    """Create / read / update for families and their members.

    Every method takes an optional keyword `uow`. Without one, the call is its
    own transaction, as before. With one, the call joins that Unit of Work: it
    runs on the unit's connection and leaves committing to the caller.
    """

    # ------------------------------------------------------------------
    # Family
    # ------------------------------------------------------------------

    def create_family(
        self,
        family_name: str,
        base_currency: Optional[str] = None,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> Family:
        """Insert a family and return it.

        If `base_currency` is omitted the column is left out of the INSERT,
        so the schema's own DEFAULT applies — the default lives in the DDL
        only, not duplicated here.
        """
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                if base_currency is None:
                    cursor = conn.execute(
                        "INSERT INTO family (family_name) VALUES (?)",
                        (family_name,),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT INTO family (family_name, base_currency) "
                        "VALUES (?, ?)",
                        (family_name, base_currency),
                    )
                row = conn.execute(_SELECT_FAMILY, (cursor.lastrowid,)).fetchone()
        return _family_from_row(row)

    def get_family(
        self, family_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[Family]:
        """Return the family, or `None` if no row has that id."""
        row = self._fetch_one(_SELECT_FAMILY, (family_id,), uow)
        return None if row is None else _family_from_row(row)

    def list_families(self, *, uow: Optional[UnitOfWork] = None) -> List[Family]:
        """Return every family, oldest `family_id` first."""
        rows = self._fetch_all(
            "SELECT family_id, family_name, base_currency FROM family "
            "ORDER BY family_id",
            (),
            uow,
        )
        return [_family_from_row(row) for row in rows]

    def update_family(
        self,
        family_id: int,
        *,
        family_name: str = _UNSET,
        base_currency: str = _UNSET,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[Family]:
        """Change the supplied fields; leave the others as they are.

        Returns the updated family, or `None` if no row has that id.
        """
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                row = conn.execute(_SELECT_FAMILY, (family_id,)).fetchone()
                if row is None:
                    return None
                current = _family_from_row(row)
                conn.execute(
                    "UPDATE family SET family_name = ?, base_currency = ? "
                    "WHERE family_id = ?",
                    (
                        current.family_name if family_name is _UNSET else family_name,
                        current.base_currency if base_currency is _UNSET else base_currency,
                        family_id,
                    ),
                )
                row = conn.execute(_SELECT_FAMILY, (family_id,)).fetchone()
        return _family_from_row(row)

    # ------------------------------------------------------------------
    # Family member
    # ------------------------------------------------------------------

    def create_family_member(
        self,
        family_id: int,
        name: str,
        relationship: str,
        date_of_birth: Optional[date] = None,
        is_active: Optional[bool] = None,
        *,
        uow: Optional[UnitOfWork] = None,
    ) -> FamilyMember:
        """Insert a member of `family_id` and return it.

        `relationship` is stored as given — the schema deliberately leaves it
        free text. If `is_active` is omitted the schema's DEFAULT applies.
        A `family_id` with no matching family raises `RepositoryIntegrityError`.
        """
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                if is_active is None:
                    cursor = conn.execute(
                        "INSERT INTO family_member "
                        "(family_id, name, relationship, date_of_birth) "
                        "VALUES (?, ?, ?, ?)",
                        (family_id, name, relationship, _date_to_db(date_of_birth)),
                    )
                else:
                    cursor = conn.execute(
                        "INSERT INTO family_member "
                        "(family_id, name, relationship, date_of_birth, is_active) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            family_id,
                            name,
                            relationship,
                            _date_to_db(date_of_birth),
                            _bool_to_db(is_active),
                        ),
                    )
                row = conn.execute(_SELECT_MEMBER, (cursor.lastrowid,)).fetchone()
        return _member_from_row(row)

    def get_family_member(
        self, member_id: int, *, uow: Optional[UnitOfWork] = None
    ) -> Optional[FamilyMember]:
        """Return the member, or `None` if no row has that id."""
        row = self._fetch_one(_SELECT_MEMBER, (member_id,), uow)
        return None if row is None else _member_from_row(row)

    def list_family_members(
        self,
        family_id: int,
        *,
        active_only: bool = False,
        uow: Optional[UnitOfWork] = None,
    ) -> List[FamilyMember]:
        """Return the members of `family_id`, oldest `member_id` first.

        An unknown `family_id` simply yields an empty list.
        """
        rows = self._fetch_all(
            "SELECT member_id, family_id, name, relationship, date_of_birth, is_active, "
            "risk_tolerance, life_stage, heir_member_id, retirement_age FROM family_member "
            "WHERE family_id = ? AND (? = 0 OR is_active = 1) "
            "ORDER BY member_id",
            (family_id, int(active_only)),
            uow,
        )
        return [_member_from_row(row) for row in rows]

    def update_family_member(
        self,
        member_id: int,
        *,
        name: str = _UNSET,
        relationship: str = _UNSET,
        date_of_birth: Optional[date] = _UNSET,
        is_active: bool = _UNSET,
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[FamilyMember]:
        """Change the supplied fields; leave the others as they are.

        `date_of_birth=None` clears the date; omitting it keeps the current
        one. `family_id` is not updatable here. Returns the updated member,
        or `None` if no row has that id.
        """
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                row = conn.execute(_SELECT_MEMBER, (member_id,)).fetchone()
                if row is None:
                    return None
                current = _member_from_row(row)
                conn.execute(
                    "UPDATE family_member "
                    "SET name = ?, relationship = ?, date_of_birth = ?, is_active = ? "
                    "WHERE member_id = ?",
                    (
                        current.name if name is _UNSET else name,
                        current.relationship if relationship is _UNSET else relationship,
                        _date_to_db(
                            current.date_of_birth
                            if date_of_birth is _UNSET
                            else date_of_birth
                        ),
                        _bool_to_db(
                            current.is_active if is_active is _UNSET else is_active
                        ),
                        member_id,
                    ),
                )
                row = conn.execute(_SELECT_MEMBER, (member_id,)).fetchone()
        return _member_from_row(row)

    def update_member_profile(
        self,
        member_id: int,
        *,
        name: str,
        relationship: str,
        date_of_birth: Optional[date],
        life_stage: Optional[str],
        heir_member_id: Optional[int],
        retirement_age: Optional[int],
        uow: Optional[UnitOfWork] = None,
    ) -> Optional[FamilyMember]:
        """Replace a member's planning details (the Overview's member edit)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE family_member SET name = ?, relationship = ?, date_of_birth = ?, "
                    "life_stage = ?, heir_member_id = ?, retirement_age = ? WHERE member_id = ?",
                    (name, relationship, _date_to_db(date_of_birth), life_stage, heir_member_id,
                     retirement_age, member_id),
                )
                row = conn.execute(_SELECT_MEMBER, (member_id,)).fetchone()
        return None if row is None else _member_from_row(row)

    def set_risk_tolerance(
        self, member_id: int, risk_tolerance: Optional[str], *, uow: Optional[UnitOfWork] = None
    ) -> Optional[FamilyMember]:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "UPDATE family_member SET risk_tolerance = ? WHERE member_id = ?",
                    (risk_tolerance, member_id),
                )
                row = conn.execute(_SELECT_MEMBER, (member_id,)).fetchone()
        return None if row is None else _member_from_row(row)


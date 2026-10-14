"""
Persistence for personal planning: the family's goals (`financial_goal`,
V6, plus `inflation_pct` from migration 014), holdings put towards a goal
(`goal_allocation`, V6), holdings earmarked for something else
(`asset_earmark`), planning preferences and dismissed suggestions
(migration 014). Plain storage only — the Financial Engine does the planning.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Dict, List, Optional, Set, Union

from .base import BaseRepository
from .errors import translate_db_errors
from .unit_of_work import UnitOfWork

Money = Union[Decimal, int, str]

_GOAL_COLUMNS = (
    "goal_id, family_id, member_id, goal_name, goal_type, target_amount, target_date, priority, "
    "status, inflation_pct"
)


@dataclass(frozen=True)
class Goal:
    goal_id: int
    family_id: int
    member_id: Optional[int]
    name: str
    goal_type: str  # EDUCATION / RETIREMENT / PROPERTY / EMERGENCY_FUND / TRAVEL / OTHER
    target_amount: Decimal  # in today's money
    target_date: date
    priority: int  # 1 high, 2 medium, 3 low (0 = not set)
    status: str  # ACTIVE / ACHIEVED / ABANDONED
    inflation_pct: Optional[Decimal]  # None = the usual rate for the type


@dataclass(frozen=True)
class GoalAllocation:
    goal_id: int
    asset_id: int
    allocated_pct: Decimal


@dataclass(frozen=True)
class PlanningPreference:
    family_id: int
    tax_slab_pct: Decimal = Decimal("30")
    emergency_months: int = 6
    min_fd_amount: Decimal = Decimal("0")
    avoid_direct_stocks: bool = False


def _goal(row: sqlite3.Row) -> Goal:
    return Goal(
        goal_id=row["goal_id"], family_id=row["family_id"], member_id=row["member_id"],
        name=row["goal_name"], goal_type=row["goal_type"],
        target_amount=Decimal(str(row["target_amount"])),
        target_date=date.fromisoformat(row["target_date"]), priority=row["priority"],
        status=row["status"],
        inflation_pct=None if row["inflation_pct"] is None else Decimal(str(row["inflation_pct"])),
    )


class PlanningRepository(BaseRepository):
    # -- goals --------------------------------------------------------------

    def list_goals(self, family_id: int, *, uow: Optional[UnitOfWork] = None) -> List[Goal]:
        rows = self._fetch_all(
            f"SELECT {_GOAL_COLUMNS} FROM financial_goal WHERE family_id = ? ORDER BY target_date, goal_id",
            (family_id,), uow,
        )
        return [_goal(r) for r in rows]

    def get_goal(self, goal_id: int, *, uow: Optional[UnitOfWork] = None) -> Optional[Goal]:
        row = self._fetch_one(f"SELECT {_GOAL_COLUMNS} FROM financial_goal WHERE goal_id = ?", (goal_id,), uow)
        return None if row is None else _goal(row)

    def save_goal(
        self, goal_id: Optional[int], *, family_id: int, member_id: Optional[int], name: str,
        goal_type: str, target_amount: Money, target_date: date, priority: int, status: str,
        inflation_pct: Optional[Money], uow: Optional[UnitOfWork] = None,
    ) -> Goal:
        """Insert (goal_id None) or update a goal; returns it."""
        values = (member_id, name, goal_type, str(target_amount), target_date.isoformat(), priority, status,
                  None if inflation_pct is None else str(inflation_pct))
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                if goal_id is None:
                    cursor = conn.execute(
                        "INSERT INTO financial_goal (family_id, member_id, goal_name, goal_type, target_amount, "
                        "target_date, priority, status, inflation_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (family_id,) + values,
                    )
                    goal_id = cursor.lastrowid
                else:
                    conn.execute(
                        "UPDATE financial_goal SET member_id = ?, goal_name = ?, goal_type = ?, target_amount = ?, "
                        "target_date = ?, priority = ?, status = ?, inflation_pct = ? WHERE goal_id = ?",
                        values + (goal_id,),
                    )
                row = conn.execute(f"SELECT {_GOAL_COLUMNS} FROM financial_goal WHERE goal_id = ?",
                                   (goal_id,)).fetchone()
        return _goal(row)

    def delete_goal(self, goal_id: int, *, uow: Optional[UnitOfWork] = None) -> bool:
        """Delete a goal and the holdings put towards it (the holdings stay)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute("DELETE FROM goal_allocation WHERE goal_id = ?", (goal_id,))
                cursor = conn.execute("DELETE FROM financial_goal WHERE goal_id = ?", (goal_id,))
        return cursor.rowcount > 0

    # -- holdings put towards goals ------------------------------------------

    def list_allocations(self, family_id: int, *, uow: Optional[UnitOfWork] = None) -> List[GoalAllocation]:
        rows = self._fetch_all(
            "SELECT a.goal_id, a.asset_id, a.allocated_pct FROM goal_allocation a "
            "JOIN financial_goal g ON g.goal_id = a.goal_id WHERE g.family_id = ? ORDER BY a.allocation_id",
            (family_id,), uow,
        )
        return [GoalAllocation(r["goal_id"], r["asset_id"], Decimal(str(r["allocated_pct"]))) for r in rows]

    # -- what a holding is for -------------------------------------------------

    def list_earmarks(self, *, uow: Optional[UnitOfWork] = None) -> Dict[int, str]:
        rows = self._fetch_all("SELECT asset_id, purpose FROM asset_earmark", (), uow)
        return {r["asset_id"]: r["purpose"] for r in rows}

    def set_asset_purpose(
        self, asset_id: int, *, purpose: Optional[str] = None, goal_id: Optional[int] = None,
        pct: Money = Decimal("100"), uow: Optional[UnitOfWork] = None,
    ) -> None:
        """Say what a holding is for: towards `goal_id` (`pct` of it), or an
        earmark `purpose` (EMERGENCY / INCOME / KEEP), or nothing (both None)."""
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute("DELETE FROM goal_allocation WHERE asset_id = ?", (asset_id,))
                conn.execute("DELETE FROM asset_earmark WHERE asset_id = ?", (asset_id,))
                if goal_id is not None:
                    conn.execute(
                        "INSERT INTO goal_allocation (goal_id, asset_id, allocated_pct, allocation_type) "
                        "VALUES (?, ?, ?, 'HARD')", (goal_id, asset_id, str(pct)),
                    )
                elif purpose is not None:
                    conn.execute("INSERT INTO asset_earmark (asset_id, purpose) VALUES (?, ?)", (asset_id, purpose))

    def clear_asset(self, asset_id: int, *, uow: Optional[UnitOfWork] = None) -> None:
        """Before a holding is deleted: drop what it was put towards."""
        self.set_asset_purpose(asset_id, uow=uow)

    # -- preferences --------------------------------------------------------

    def get_preference(self, family_id: int, *, uow: Optional[UnitOfWork] = None) -> PlanningPreference:
        row = self._fetch_one(
            "SELECT family_id, tax_slab_pct, emergency_months, min_fd_amount, avoid_direct_stocks "
            "FROM planning_preference WHERE family_id = ?", (family_id,), uow,
        )
        if row is None:
            return PlanningPreference(family_id)
        return PlanningPreference(
            family_id=family_id, tax_slab_pct=Decimal(str(row["tax_slab_pct"])),
            emergency_months=row["emergency_months"], min_fd_amount=Decimal(str(row["min_fd_amount"])),
            avoid_direct_stocks=bool(row["avoid_direct_stocks"]),
        )

    def save_preference(self, pref: PlanningPreference, *, uow: Optional[UnitOfWork] = None) -> None:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "INSERT INTO planning_preference (family_id, tax_slab_pct, emergency_months, min_fd_amount, "
                    "avoid_direct_stocks) VALUES (?, ?, ?, ?, ?) ON CONFLICT (family_id) DO UPDATE SET "
                    "tax_slab_pct = excluded.tax_slab_pct, emergency_months = excluded.emergency_months, "
                    "min_fd_amount = excluded.min_fd_amount, avoid_direct_stocks = excluded.avoid_direct_stocks",
                    (pref.family_id, str(pref.tax_slab_pct), pref.emergency_months, str(pref.min_fd_amount),
                     int(pref.avoid_direct_stocks)),
                )

    # -- whose money may fund goals ----------------------------------------

    def list_member_policies(self, *, uow: Optional[UnitOfWork] = None) -> Dict[int, str]:
        rows = self._fetch_all("SELECT member_id, goal_money FROM planning_member_policy", (), uow)
        return {r["member_id"]: r["goal_money"] for r in rows}

    def set_member_policy(self, member_id: int, goal_money: str, *, uow: Optional[UnitOfWork] = None) -> None:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                conn.execute(
                    "INSERT INTO planning_member_policy (member_id, goal_money) VALUES (?, ?) "
                    "ON CONFLICT (member_id) DO UPDATE SET goal_money = excluded.goal_money",
                    (member_id, goal_money),
                )

    # -- dismissed suggestions ---------------------------------------------

    def list_dismissed(self, family_id: int, *, uow: Optional[UnitOfWork] = None) -> Set[str]:
        rows = self._fetch_all("SELECT move_key FROM planning_dismissed WHERE family_id = ?", (family_id,), uow)
        return {r["move_key"] for r in rows}

    def set_dismissed(
        self, family_id: int, move_key: str, dismissed: bool, on: date, *, uow: Optional[UnitOfWork] = None
    ) -> None:
        with translate_db_errors():
            with self._db.scope(uow) as conn:
                if dismissed:
                    conn.execute(
                        "INSERT INTO planning_dismissed (family_id, move_key, dismissed_on) VALUES (?, ?, ?) "
                        "ON CONFLICT (family_id, move_key) DO NOTHING",
                        (family_id, move_key, on.isoformat()),
                    )
                else:
                    conn.execute("DELETE FROM planning_dismissed WHERE family_id = ? AND move_key = ?",
                                 (family_id, move_key))

"""
Due-date reminders: which FD maturities and insurance premiums need an email
today, and in which window.

The rule (pure date logic — no I/O, no SQL):

  * Two windows before each due date: T-15 (15 to 4 days before) and T-3
    (3 days before, down to the due date itself).
  * One reminder per item per window, however many times the app is opened
    inside it. `sent_key` is what the caller records once a reminder has
    gone out; anything already recorded is not sent again.
  * A window that was never reached (the app was not opened in it) is simply
    skipped: opening the app 2 days before a due date sends only the T-3
    reminder, not a late T-15 one as well.
  * SIPs are never reminded about — only FD maturities and premiums.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, List, Optional, Set

T15 = "T15"
T3 = "T3"
FD_MATURITY = "FD_MATURITY"
PREMIUM = "PREMIUM"


@dataclass(frozen=True)
class DueItem:
    """Something with a due date: an FD maturing or a premium falling due.
    `key` identifies the item (not the date), e.g. "FD:12" / "POLICY:4"."""

    key: str
    kind: str  # FD_MATURITY | PREMIUM
    owner_name: str
    title: str  # "HDFC Bank FD" / "LIC — Jeevan Anand"
    due_date: date
    amount: Decimal  # amount at maturity / premium amount


@dataclass(frozen=True)
class Reminder:
    item: DueItem
    window: str  # T15 | T3
    days_left: int

    @property
    def sent_key(self) -> str:
        return sent_key(self.item, self.window)


def sent_key(item: DueItem, window: str) -> str:
    """The once-only key: the item, the due date it is for, and the window —
    so next year's premium on the same policy is reminded about afresh."""
    return f"{item.key}:{item.due_date.isoformat()}:{window}"


def reminder_window(due_date: date, today: date) -> Optional[str]:
    days_left = (due_date - today).days
    if 0 <= days_left <= 3:
        return T3
    if 3 < days_left <= 15:
        return T15
    return None


def reminders_due(items: Iterable[DueItem], today: date, already_sent: Set[str]) -> List[Reminder]:
    """The reminders to send now, soonest due first."""
    due: List[Reminder] = []
    for item in items:
        window = reminder_window(item.due_date, today)
        if window is None or sent_key(item, window) in already_sent:
            continue
        due.append(Reminder(item=item, window=window, days_left=(item.due_date - today).days))
    due.sort(key=lambda r: (r.item.due_date, r.item.owner_name, r.item.title))
    return due

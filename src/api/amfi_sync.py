"""
Daily MF NAV sync from AMFI's public NAV file.

AMFI publishes every scheme's latest NAV as one semicolon-delimited text file
(no login, no API key):

    Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date
    135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;29.6905;25-Sep-2026

interleaved with blank lines and section-header lines (fund category, AMC
name) that are not data. Each data row carries up to two ISINs; either can
be `-`. The date is the NAV's own date — typically the previous trading day,
further behind over weekends — so it is recorded as-is, never as "today".

Holdings are matched by ISIN only. Matching by scheme name across AMCs'
naming variants could silently attach the wrong fund's price to a holding,
which is worse than not syncing it; a holding with no ISIN is reported as
unmatched instead.

This lives in `api`, not `financial_engine`, on purpose: fetching an
external file is network I/O, and the Engine has none — every Engine test
runs offline. This module only reads holdings and records NAVs through
`FinancialEngine`'s existing methods (`list_mfs`, `record_mf_valuation`); it
imports neither `repository` nor `sqlite3`.

"Last synced" bookkeeping is a small JSON file next to the database, the
same pattern as `auth.py`'s credential file: it is operational state, not a
financial fact, so it stays out of SQLite.
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from financial_engine import FinancialEngine

AMFI_NAV_URL = "https://portal.amfiindia.com/spages/NAVAll.txt"
_FIELD_COUNT = 8
_NAV_DATE_FORMAT = "%d-%b-%Y"

NavLookup = Dict[str, Tuple[Decimal, date]]
Fetcher = Callable[[], str]


# ----------------------------------------------------------------------
# Parsing: pure, no network
# ----------------------------------------------------------------------


def parse_amfi_nav_file(raw_text: str) -> NavLookup:
    """Return `{isin: (nav, nav_date)}` for every usable data row.

    Skips the header, blank lines, section headers, and any row whose NAV or
    date does not parse (AMFI occasionally publishes `N.A.` for a NAV). A
    row with two ISINs maps both to the same NAV.
    """
    lookup: NavLookup = {}
    for line in raw_text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != _FIELD_COUNT or not parts[0].isdigit():
            continue
        isin_growth, isin_reinvest, raw_nav, raw_date = parts[1], parts[2], parts[6], parts[7]
        try:
            nav = Decimal(raw_nav)
            nav_date = datetime.strptime(raw_date, _NAV_DATE_FORMAT).date()
        except (InvalidOperation, ValueError):
            continue
        if nav <= 0:
            continue
        for isin in (isin_growth, isin_reinvest):
            if isin and isin != "-":
                lookup[isin.upper()] = (nav, nav_date)
    return lookup


def parse_amfi_scheme_names(raw_text: str) -> Dict[str, str]:
    """Return `{isin: scheme name}` for every data row — used to name a fund
    that a statement lists only by ISIN."""
    names: Dict[str, str] = {}
    for line in raw_text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != _FIELD_COUNT or not parts[0].isdigit() or not parts[3]:
            continue
        for isin in (parts[1], parts[2]):
            if isin and isin != "-":
                names[isin.upper()] = parts[3]
    return names


# ----------------------------------------------------------------------
# Fetching: the only network call, kept as small as possible
# ----------------------------------------------------------------------


def fetch_amfi_nav_file(timeout: float = 20) -> str:
    """Download AMFI's current NAV file. Raises on any network failure."""
    request = urllib.request.Request(AMFI_NAV_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


# ----------------------------------------------------------------------
# Sync state: when did we last try, when did it last work
# ----------------------------------------------------------------------


class SyncStateStore:
    """Last-attempt / last-success bookkeeping for the automatic daily sync."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}  # a corrupt state file must never break the dashboard

    def last_attempt_date(self) -> Optional[date]:
        raw = self.read().get("last_attempt_date")
        return date.fromisoformat(raw) if raw else None

    def record(self, today: date, result: "SyncResult") -> None:
        state = self.read()
        state["last_attempt_date"] = today.isoformat()
        state["last_error"] = result.error
        if result.error is None:
            state["last_success_date"] = today.isoformat()
            if result.nav_date is not None:
                state["last_nav_date"] = result.nav_date.isoformat()
        self._path.write_text(json.dumps(state), encoding="utf-8")


# ----------------------------------------------------------------------
# The sync itself
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    """What one sync did. `updated` / `unchanged` / `unmatched` name holdings
    as "Scheme (Folio)"; `unmatched` says why."""

    attempted: bool
    nav_date: Optional[date] = None
    updated: Tuple[str, ...] = ()
    unchanged: Tuple[str, ...] = ()
    unmatched: Tuple[str, ...] = ()
    error: Optional[str] = None


def _label(mf) -> str:
    return f"{mf.scheme_name} ({mf.folio_number})"


def sync_family_mf_navs(
    engine: FinancialEngine,
    family_id: int,
    *,
    fetcher: Fetcher = fetch_amfi_nav_file,
) -> SyncResult:
    """Fetch AMFI's file and record a fresh NAV for every matching holding.

    A holding already carrying this exact AMFI NAV for this exact date is
    left alone, so syncing twice in a day adds no duplicate rows. Any
    failure — network, parsing, a holding rejecting the write — is returned
    in `error`, never raised: the caller decides whether to show it.
    """
    try:
        lookup = parse_amfi_nav_file(fetcher())
    except Exception as exc:  # noqa: BLE001 - any fetch failure is reported, not raised
        return SyncResult(attempted=True, error=f"could not fetch AMFI NAVs: {exc}")
    if not lookup:
        return SyncResult(attempted=True, error="AMFI file contained no usable NAV rows")

    updated: List[str] = []
    unchanged: List[str] = []
    unmatched: List[str] = []
    nav_dates: List[date] = []
    try:
        for member in engine.list_family_members(family_id):
            for mf in engine.list_mfs(member.member_id):
                if not mf.isin:
                    unmatched.append(f"{_label(mf)}: no ISIN recorded")
                    continue
                match = lookup.get(mf.isin.strip().upper())
                if match is None:
                    unmatched.append(f"{_label(mf)}: ISIN {mf.isin} not in AMFI file")
                    continue
                nav, nav_date = match
                nav_dates.append(nav_date)
                already = (
                    mf.current_nav_source == "AMFI"
                    and mf.current_nav_date == nav_date
                    and mf.current_nav == nav
                )
                if already:
                    unchanged.append(_label(mf))
                    continue
                engine.record_mf_valuation(
                    asset_id=mf.asset_id, valuation_date=nav_date, nav=nav, source="AMFI"
                )
                updated.append(_label(mf))
    except Exception as exc:  # noqa: BLE001
        return SyncResult(
            attempted=True, updated=tuple(updated), unchanged=tuple(unchanged),
            unmatched=tuple(unmatched), error=f"sync stopped partway: {exc}",
        )

    return SyncResult(
        attempted=True,
        nav_date=max(nav_dates) if nav_dates else None,
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        unmatched=tuple(unmatched),
    )


def sync_if_stale(
    engine: FinancialEngine,
    family_id: int,
    state: SyncStateStore,
    *,
    today: Optional[date] = None,
    fetcher: Fetcher = fetch_amfi_nav_file,
) -> SyncResult:
    """The automatic "on open" sync: at most one attempt per calendar day.

    A failed attempt also counts, so an unreachable AMFI costs one slow page
    load per day rather than one per page load; the manual sync button is
    always available to retry.
    """
    today = today or date.today()
    if state.last_attempt_date() == today:
        return SyncResult(attempted=False)
    result = sync_family_mf_navs(engine, family_id, fetcher=fetcher)
    state.record(today, result)
    return result

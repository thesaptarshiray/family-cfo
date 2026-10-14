"""Tests for `api.amfi_sync`: parsing AMFI's NAV file and syncing holdings.

No network: the sample below is real lines copied from AMFI's live
NAVAll.txt (fetched 2026-09-28), and every sync test injects it through the
`fetcher` parameter instead of downloading anything.
"""

from datetime import date
from decimal import Decimal

import pytest

from api.amfi_sync import (
    SyncStateStore,
    parse_amfi_nav_file,
    sync_family_mf_navs,
    sync_if_stale,
)
from financial_engine import FinancialEngine
from repository.database import Database
from repository.schema import initialize_schema

SAMPLE = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Children’s Fund - Childrens' Fund)

Axis Mutual Fund

135762;INF846K01WO1;-;Axis Children's Fund;Direct Plan;Growth Option;29.6905;25-Sep-2026
135763;INF846K01WS2;INF846K01WQ6;Axis Children's Fund;Direct Plan;IDCW Option;27.4020;25-Sep-2026

HDFC Mutual Fund

128628;INF179KA1JC4;-;HDFC Banking and PSU Debt Fund;Regular Plan;Growth Option;24.3321;25-Sep-2026
123689;INF174K01KI5;INF174K01KJ3;Kotak Banking and PSU Debt Fund;Direct Plan;Annual Dividend;17.1427;15-Sep-2017
999999;INF000BAD001;-;Broken Fund;Direct Plan;Growth;N.A.;25-Sep-2026
999998;INF000BAD002;-;Broken Date Fund;Direct Plan;Growth;10.0000;not-a-date
"""


# ----------------------------------------------------------------------
# parse_amfi_nav_file
# ----------------------------------------------------------------------


def test_parses_data_rows_and_skips_header_blank_and_section_lines():
    lookup = parse_amfi_nav_file(SAMPLE)
    assert lookup["INF846K01WO1"] == (Decimal("29.6905"), date(2026, 9, 25))
    assert lookup["INF179KA1JC4"] == (Decimal("24.3321"), date(2026, 9, 25))
    assert "Scheme Code" not in lookup and "-" not in lookup


def test_both_isins_on_a_row_map_to_the_same_nav():
    lookup = parse_amfi_nav_file(SAMPLE)
    assert lookup["INF846K01WS2"] == lookup["INF846K01WQ6"] == (
        Decimal("27.4020"), date(2026, 9, 25)
    )


def test_keeps_the_rows_own_date_even_when_stale():
    # AMFI still lists some discontinued plans with their last NAV date.
    assert parse_amfi_nav_file(SAMPLE)["INF174K01KI5"][1] == date(2017, 9, 15)


def test_rows_with_an_unparseable_nav_or_date_are_skipped():
    lookup = parse_amfi_nav_file(SAMPLE)
    assert "INF000BAD001" not in lookup  # NAV "N.A."
    assert "INF000BAD002" not in lookup  # date "not-a-date"


def test_empty_or_garbage_input_gives_an_empty_lookup():
    assert parse_amfi_nav_file("") == {}
    assert parse_amfi_nav_file("<html>Object Moved</html>") == {}


def test_windows_line_endings_are_handled():
    lookup = parse_amfi_nav_file(SAMPLE.replace("\n", "\r\n"))
    assert lookup["INF846K01WO1"][0] == Decimal("29.6905")


# ----------------------------------------------------------------------
# sync_family_mf_navs
# ----------------------------------------------------------------------


@pytest.fixture
def family(tmp_path):
    database = Database(tmp_path / "sync.db")
    initialize_schema(database)
    engine = FinancialEngine(database)
    fam = engine.create_family("Ray")
    me = engine.create_family_member(fam.family_id, "Saptarshi", "self")
    spouse = engine.create_family_member(fam.family_id, "Spouse", "spouse")
    return engine, fam, me, spouse


def _mf(engine, member_id, isin, folio="F-1"):
    return engine.create_mutual_fund(
        member_id=member_id, amc="Axis", scheme_name="Axis Children's Fund",
        folio_number=folio, isin=isin, initial_txn_date=date(2025, 1, 15),
        initial_txn_type="SIP", initial_units=Decimal("100"),
        initial_nav=Decimal("20"), initial_amount=Decimal("2000"),
    )


def test_sync_records_the_amfi_nav_for_every_matching_holding_across_members(family):
    engine, fam, me, spouse = family
    mine = _mf(engine, me.member_id, "INF846K01WO1")
    theirs = _mf(engine, spouse.member_id, "INF179KA1JC4", folio="F-2")

    result = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: SAMPLE)

    assert result.error is None and len(result.updated) == 2
    assert result.nav_date == date(2026, 9, 25)
    mine_now = engine.list_mfs(me.member_id)[0]
    assert mine_now.current_nav == Decimal("29.6905")
    assert mine_now.current_nav_source == "AMFI"
    assert mine_now.current_nav_date == date(2026, 9, 25)
    assert mine_now.current_value == Decimal("100") * Decimal("29.6905")
    assert engine.list_mfs(spouse.member_id)[0].current_nav == Decimal("24.3321")


def test_matching_ignores_isin_case_and_surrounding_whitespace(family):
    engine, fam, me, _ = family
    _mf(engine, me.member_id, "  inf846k01wo1 ")
    result = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: SAMPLE)
    assert len(result.updated) == 1


def test_holdings_without_an_isin_or_not_in_the_file_are_reported_unmatched(family):
    engine, fam, me, _ = family
    _mf(engine, me.member_id, None, folio="NO-ISIN")
    _mf(engine, me.member_id, "INF999ZZ9999", folio="UNKNOWN")

    result = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: SAMPLE)

    assert result.updated == ()
    assert len(result.unmatched) == 2
    assert any("no ISIN" in u for u in result.unmatched)
    assert any("not in AMFI file" in u for u in result.unmatched)
    # untouched: still the NAV from the first transaction
    assert all(m.current_nav == Decimal("20") for m in engine.list_mfs(me.member_id))


def test_syncing_twice_with_the_same_data_changes_nothing_the_second_time(family):
    engine, fam, me, _ = family
    mf = _mf(engine, me.member_id, "INF846K01WO1")

    first = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: SAMPLE)
    second = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: SAMPLE)

    assert len(first.updated) == 1
    assert second.updated == () and len(second.unchanged) == 1
    history = engine._valuations.list_valuations(mf.asset_id)
    assert sum(1 for v in history if v.source == "AMFI") == 1  # no duplicate rows


def test_a_fetch_failure_is_returned_as_an_error_not_raised(family):
    engine, fam, me, _ = family
    _mf(engine, me.member_id, "INF846K01WO1")

    def boom():
        raise OSError("network unreachable")

    result = sync_family_mf_navs(engine, fam.family_id, fetcher=boom)

    assert result.attempted and "network unreachable" in result.error
    assert engine.list_mfs(me.member_id)[0].current_nav == Decimal("20")


def test_a_file_with_no_usable_rows_is_an_error(family):
    engine, fam, _, _ = family
    result = sync_family_mf_navs(engine, fam.family_id, fetcher=lambda: "<html>moved</html>")
    assert "no usable NAV rows" in result.error


# ----------------------------------------------------------------------
# sync_if_stale and the state file
# ----------------------------------------------------------------------


def test_auto_sync_runs_at_most_once_per_day(family, tmp_path):
    engine, fam, me, _ = family
    _mf(engine, me.member_id, "INF846K01WO1")
    state = SyncStateStore(tmp_path / "state.json")
    calls = []

    def fetcher():
        calls.append(1)
        return SAMPLE

    day = date(2026, 9, 29)
    assert sync_if_stale(engine, fam.family_id, state, today=day, fetcher=fetcher).attempted
    assert not sync_if_stale(engine, fam.family_id, state, today=day, fetcher=fetcher).attempted
    assert len(calls) == 1
    assert sync_if_stale(engine, fam.family_id, state, today=date(2026, 9, 30), fetcher=fetcher).attempted
    assert len(calls) == 2


def test_a_failed_auto_sync_still_counts_as_todays_attempt(family, tmp_path):
    engine, fam, _, _ = family
    state = SyncStateStore(tmp_path / "state.json")
    calls = []

    def failing():
        calls.append(1)
        raise OSError("AMFI down")

    day = date(2026, 9, 29)
    sync_if_stale(engine, fam.family_id, state, today=day, fetcher=failing)
    sync_if_stale(engine, fam.family_id, state, today=day, fetcher=failing)
    assert len(calls) == 1  # an unreachable AMFI costs one slow load a day, not every load
    saved = state.read()
    assert "AMFI down" in saved["last_error"]
    assert "last_success_date" not in saved


def test_state_records_success_and_the_nav_date(family, tmp_path):
    engine, fam, me, _ = family
    _mf(engine, me.member_id, "INF846K01WO1")
    state = SyncStateStore(tmp_path / "state.json")

    sync_if_stale(engine, fam.family_id, state, today=date(2026, 9, 29), fetcher=lambda: SAMPLE)

    saved = state.read()
    assert saved["last_success_date"] == "2026-09-29"
    assert saved["last_nav_date"] == "2026-09-25"
    assert saved["last_error"] is None


def test_a_corrupt_state_file_is_treated_as_never_synced(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    assert SyncStateStore(path).last_attempt_date() is None

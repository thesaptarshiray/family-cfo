"""
Tests for `repository.schema` — proving the approved V6 DDL actually
initializes a fresh database correctly.
"""

import re
import sqlite3

import pytest

from repository.database import Database
from repository.schema import (
    DEFAULT_DDL_PATH,
    MIGRATIONS_DIR,
    get_schema_version,
    get_table_names,
    initialize_schema,
    list_migrations,
    migrate_schema,
    pending_migrations,
)

# The tables the approved V6 DDL (plus approved migrations) defines (excludes SQLite's own
# `sqlite_sequence` bookkeeping table). Hardcoded here deliberately: if the
# DDL file changes table names without this list being updated too, these
# tests should fail loudly rather than silently pass on a drifted schema.
EXPECTED_TABLES = [
    "account_ownership",
    "asset",
    "asset_earmark",
    "asset_ownership",
    "asset_valuation",
    "bank_account",
    "bulk_import_batch",
    "bulk_import_error",
    "calculation_input_ref",
    "calculation_log",
    "cash_flow_item",
    "family",
    "family_member",
    "fd_holding",
    "financial_goal",
    "goal_allocation",
    "gold_holding",
    "insurance_policy",
    "insurance_premium_payment",
    "investment_lot",
    "liability",
    "liability_ownership",
    "loan_detail",
    "loan_disbursement",
    "loan_payment",
    "loan_prepayment",
    "loan_rate_change",
    "lot_consumption",
    "mf_holding",
    "mf_transaction",
    "nps_account",
    "planning_dismissed",
    "planning_member_policy",
    "planning_preference",
    "property_holding",
    "recurring_rule",
    "retirement_account",
    "scenario",
    "scenario_assumption",
    "scenario_result",
    "stock_holding",
    "stock_info",
    "stock_transaction",
    "succession_check",
    "transaction",
    "txn_category",
]


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "test.db"


def test_ddl_file_exists():
    assert DEFAULT_DDL_PATH.exists(), (
        f"Expected the approved V6 DDL at {DEFAULT_DDL_PATH}"
    )


def test_initialize_schema_succeeds_on_empty_database(db_path):
    database = Database(db_path)
    initialize_schema(database)  # should not raise


def test_all_expected_tables_exist_after_initialization(db_path):
    database = Database(db_path)
    initialize_schema(database)

    actual_tables = get_table_names(database)

    assert sorted(actual_tables) == sorted(EXPECTED_TABLES)


def test_foreign_keys_enabled_after_schema_initialization(db_path):
    database = Database(db_path)
    initialize_schema(database)

    assert database.foreign_keys_enabled() is True


def test_foreign_key_enforcement_actually_active_against_real_schema(db_path):
    """Not just that the PRAGMA is on, but that SQLite actually enforces it
    against the real schema — e.g. bank_account.member_id must reference an
    existing family_member."""
    database = Database(db_path)
    initialize_schema(database)

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            conn.execute(
                "INSERT INTO bank_account "
                "(member_id, bank_name, account_type, account_number_masked, "
                " opening_balance, current_balance, is_active) "
                "VALUES (999, 'Test Bank', 'SAVINGS', '1234', 0, 0, 1);"
            )


def test_initialize_schema_is_not_idempotent(db_path):
    """Re-running against an already-initialized database should raise,
    not silently succeed — see the docstring in schema.py for why this is
    intentional rather than a bug to fix."""
    database = Database(db_path)
    initialize_schema(database)

    with pytest.raises(sqlite3.OperationalError):
        initialize_schema(database)


def test_reopening_database_after_schema_init_still_has_all_tables(db_path):
    database = Database(db_path)
    initialize_schema(database)

    reopened_database = Database(db_path)
    actual_tables = get_table_names(reopened_database)

    assert sorted(actual_tables) == sorted(EXPECTED_TABLES)


# ----------------------------------------------------------------------
# Migrations
# ----------------------------------------------------------------------

def _columns(database, table):
    conn = database.connect()
    try:
        return [(r[1], r[2]) for r in conn.execute(f"PRAGMA table_info({table})")]
    finally:
        conn.close()


_V6_LOAN_DETAIL = """CREATE TABLE loan_detail (
    liability_id              INTEGER PRIMARY KEY REFERENCES liability(liability_id) ON DELETE CASCADE ON UPDATE CASCADE,
    interest_rate                DECIMAL(5,2) NOT NULL,
    interest_type                   TEXT NOT NULL CHECK (interest_type IN ('FIXED','FLOATING')),
    original_tenure_months             INTEGER NOT NULL CHECK (original_tenure_months > 0),
    emi_amount                            DECIMAL(18,2) NOT NULL CHECK (emi_amount > 0),
    emi_start_date                           DATE NOT NULL
);
"""


def _pre_migration_ddl(tmp_path):
    """The DDL as it was before any migration: each migration's change
    undone, so a database built from it looks like one created back then."""
    ddl = DEFAULT_DDL_PATH.read_text(encoding="utf-8")
    # 015: the member-policy block, verbatim from the migration file
    mig15 = (MIGRATIONS_DIR / "015_goal_money_policy.sql").read_text(encoding="utf-8")
    block15 = mig15[mig15.index("-- ===="):]
    assert block15 in ddl
    ddl = ddl.replace(block15, "")
    # 014: the personal-planning block, verbatim from the migration file
    mig14 = (MIGRATIONS_DIR / "014_personal_planning.sql").read_text(encoding="utf-8")
    block = mig14[mig14.index("-- ===="):]
    assert block in ddl
    ddl = ddl.replace(block, "")
    ddl, undone = re.subn(r",\n\s*inflation_pct\b[^\n]*migration 014[^\n]*", "", ddl)
    assert undone == 1
    # 013: cash_flow_item without 'ONCE' and its trailing spent_on column
    before = ddl
    ddl = ddl.replace("'ANNUAL','ONCE')),  -- ONCE: migration 013", "'ANNUAL')),")
    ddl, undone = re.subn(r",  -- migration 006\n\s*spent_on\b[^\n]*", "  -- migration 006", ddl)
    assert undone == 1 and ddl != before
    # 012: insurance_policy lost its trailing premium_paying_term_years column
    ddl, undone = re.subn(r",  -- migration 011\n\s*premium_paying_term_years\b[^\n]*", "  -- migration 011", ddl)
    assert undone == 1
    # 011: the succession_check block; family_member's and insurance_policy's trailing columns
    mig11 = (MIGRATIONS_DIR / "011_people_and_policies.sql").read_text(encoding="utf-8")
    block = mig11[mig11.index("\n-- ===="):]
    assert block in ddl
    ddl = ddl.replace(block, "")
    ddl, undone = re.subn(
        r"\n\s*life_stage\b[^\n]*\n\s*heir_member_id\b[^\n]*\n\s*retirement_age\b[^\n]*", "", ddl
    )
    assert undone == 1
    ddl, undone = re.subn(r"\n\s*plan_name\b[^\n]*\n\s*nominee\b[^\n]*", "", ddl)
    assert undone == 1
    ddl = ddl.replace("'AGGRESSIVE')),  -- migration 007", "'AGGRESSIVE'))  -- migration 007")
    ddl = re.sub(r"(accrued_bonus\s+DECIMAL\(18,2\)),  --", r"\1   --", ddl)
    # 010: mf_holding lost its trailing broker column
    ddl, undone = re.subn(r",\n\s*broker\s+TEXT\s+-- migration 010[^\n]*\n", "\n", ddl)
    assert undone == 1
    # 009: mf_transaction and stock_transaction each lost their two trailing columns
    ddl, undone = re.subn(
        r",\n\s*is_opening_balance\b[^\n]*\n\s*import_batch_id\b[^\n]*\n", "\n", ddl
    )
    assert undone == 2
    # 008: loan_detail as V6 first defined it, and no loan_disbursement
    mig8 = (MIGRATIONS_DIR / "008_loan_detail_and_disbursement.sql").read_text(encoding="utf-8")
    block = mig8[mig8.index("CREATE TABLE loan_detail"):]
    assert block in ddl
    ddl = ddl.replace(block, _V6_LOAN_DETAIL)
    # 001: accrued_bonus is insurance_policy's last column (with the comma before it)
    ddl, undone = re.subn(r",(\s*\n\s*accrued_bonus\b[^\n]*)\n", "\n", ddl)
    assert undone == 1
    # 002: the stock_transaction block is the migration file's text, verbatim
    block = (MIGRATIONS_DIR / "002_stock_transaction.sql").read_text(encoding="utf-8")
    assert block in ddl
    ddl = ddl.replace(block, "")
    # 003: purity is gold_holding's last column (with the comma before it)
    ddl, undone = re.subn(r",(\s*\n\s*purity\b[^\n]*)\n", "\n", ddl)
    assert undone == 1
    # 004: fd_holding.payout_type did not allow MONTHLY
    monthly = "payout_type IN ('MATURITY','QUARTERLY','MONTHLY','MANUAL')"
    assert ddl.count(monthly) == 1
    ddl = ddl.replace(monthly, "payout_type IN ('MATURITY','QUARTERLY','MANUAL')")
    # 007: the stock_info block (the migration file up to its ALTER), and
    # family_member's trailing risk_tolerance column
    mig7 = (MIGRATIONS_DIR / "007_stock_info_and_risk_tolerance.sql").read_text(encoding="utf-8")
    block = mig7[: mig7.index("-- Each member's own risk tolerance")]
    assert block in ddl
    ddl = ddl.replace(block, "")
    ddl, undone = re.subn(r",\n\s*risk_tolerance\b[^\n]*\n", "\n", ddl)
    assert undone == 1
    # 006 (undone before 005, whose block it edits): bank_account and
    # cash_flow_item each lost their trailing added columns
    ddl, undone = re.subn(
        r",\n\s*is_default\b[^\n]*\n\s*balance_as_of\b[^\n]*\n", "\n", ddl
    )
    assert undone == 1
    ddl, undone = re.subn(
        r",\n\s*category\b[^\n]*\n\s*payment_mode\b[^\n]*\n\s*account_id\b[^\n]*\n", "\n", ddl
    )
    assert undone == 1
    # 005: the cash_flow_item block is the migration file's text, verbatim
    block = (MIGRATIONS_DIR / "005_cash_flow_item.sql").read_text(encoding="utf-8")
    assert block in ddl
    ddl = ddl.replace(block, "")
    path = tmp_path / "v0.sql"
    path.write_text(ddl, encoding="utf-8")
    return path


def test_a_fresh_database_starts_at_the_latest_version(db_path):
    database = Database(db_path)
    initialize_schema(database)
    assert get_schema_version(database) == len(list_migrations()) >= 1
    assert pending_migrations(database) == []
    assert migrate_schema(database) == []


def test_migrating_an_old_database_matches_a_fresh_one_and_keeps_its_data(tmp_path):
    old = Database(tmp_path / "old.db")
    initialize_schema(old, _pre_migration_ddl(tmp_path))
    conn = old.connect()
    conn.execute("PRAGMA user_version = 0")
    conn.execute("INSERT INTO family (family_name) VALUES ('Ray')")
    conn.commit()
    conn.close()
    assert "accrued_bonus" not in dict(_columns(old, "insurance_policy"))
    assert "stock_transaction" not in get_table_names(old)

    applied = migrate_schema(old)

    assert applied == [p.name for p in list_migrations()]
    assert get_schema_version(old) == len(list_migrations())
    fresh = Database(tmp_path / "fresh.db")
    initialize_schema(fresh)
    assert _columns(old, "insurance_policy") == _columns(fresh, "insurance_policy")
    assert sorted(get_table_names(old)) == sorted(get_table_names(fresh))
    assert _columns(old, "stock_transaction") == _columns(fresh, "stock_transaction")
    assert _columns(old, "gold_holding") == _columns(fresh, "gold_holding")
    assert _columns(old, "fd_holding") == _columns(fresh, "fd_holding")
    assert _columns(old, "bank_account") == _columns(fresh, "bank_account")
    assert _columns(old, "cash_flow_item") == _columns(fresh, "cash_flow_item")
    assert _columns(old, "family_member") == _columns(fresh, "family_member")
    assert _columns(old, "stock_info") == _columns(fresh, "stock_info")
    assert _columns(old, "loan_detail") == _columns(fresh, "loan_detail")
    assert _columns(old, "loan_disbursement") == _columns(fresh, "loan_disbursement")
    assert _columns(old, "mf_transaction") == _columns(fresh, "mf_transaction")
    assert _columns(old, "mf_holding") == _columns(fresh, "mf_holding")
    assert _columns(old, "insurance_policy") == _columns(fresh, "insurance_policy")
    assert _columns(old, "succession_check") == _columns(fresh, "succession_check")
    assert _columns(old, "financial_goal") == _columns(fresh, "financial_goal")
    assert _columns(old, "planning_preference") == _columns(fresh, "planning_preference")
    assert _columns(old, "stock_transaction") == _columns(fresh, "stock_transaction")
    conn = old.connect()
    assert conn.execute("SELECT family_name FROM family").fetchone()[0] == "Ray"
    conn.close()
    assert migrate_schema(old) == []  # nothing left to apply


def test_a_failing_migration_rolls_back_and_keeps_the_version(tmp_path):
    database = Database(tmp_path / "m.db")
    initialize_schema(database)
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for p in list_migrations():
        (migrations / p.name).write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    start = get_schema_version(database)
    (migrations / f"{start + 1:03d}_bad.sql").write_text(
        "CREATE TABLE half_done (x INTEGER);\nALTER TABLE no_such_table ADD COLUMN y INTEGER;\n",
        encoding="utf-8",
    )

    with pytest.raises(Exception):
        migrate_schema(database, migrations)

    assert get_schema_version(database) == start
    assert "half_done" not in get_table_names(database)


def test_migration_004_keeps_existing_fds_and_allows_monthly_payout(tmp_path):
    old = Database(tmp_path / "old.db")
    initialize_schema(old, _pre_migration_ddl(tmp_path))
    conn = old.connect()
    conn.execute("PRAGMA user_version = 0")
    conn.execute("INSERT INTO family (family_name) VALUES ('Ray')")
    conn.execute("INSERT INTO family_member (family_id, name, relationship) VALUES (1, 'A', 'self')")
    conn.execute("INSERT INTO asset (member_id, category, name, purchase_date) VALUES (1, 'FD', 'x', '2025-03-14')")
    conn.execute(
        "INSERT INTO fd_holding (asset_id, fd_number, bank_name, principal, interest_rate, "
        "issue_date, maturity_date, payout_type) VALUES (1, 'F1', 'ICICI', 2500000, 7.96, "
        "'2025-03-14', '2026-09-13', 'QUARTERLY')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):  # not allowed before the migration
        conn.execute("UPDATE fd_holding SET payout_type = 'MONTHLY'")
    conn.close()

    migrate_schema(old)

    conn = old.connect()
    try:
        row = conn.execute("SELECT fd_number, bank_name, principal, payout_type FROM fd_holding").fetchone()
        assert tuple(row) == ("F1", "ICICI", 2500000, "QUARTERLY")
        conn.execute("UPDATE fd_holding SET payout_type = 'MONTHLY'")
        conn.commit()
        indexes = [r[1] for r in conn.execute("PRAGMA index_list(fd_holding)")]
        assert "idx_fd_holding_maturity" in indexes
    finally:
        conn.close()

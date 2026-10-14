"""
Schema initialization: loads and applies the approved V6 DDL.

This module's only job is to get an empty SQLite file to match
`schema/family-finance-app-v6-ddl.sql` exactly. It contains no business
logic and makes no decisions about the schema's content — the DDL file is
the single source of truth; this module just executes it.

Nothing here is a general-purpose query interface: `get_table_names()` runs
one fixed, hardcoded introspection query. Neither function accepts
caller-supplied SQL, by design (see requirement 8 in the block-1 spec).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Union

from .database import Database

# The approved V6 DDL, checked into the project alongside the code it
# initializes. Resolved relative to this file rather than the working
# directory, so `initialize_schema()` works regardless of where the caller
# was run from.
DEFAULT_DDL_PATH = (
    Path(__file__).resolve().parents[2] / "schema" / "family-finance-app-v6-ddl.sql"
)


def initialize_schema(
    database: Database, ddl_path: Union[str, Path] = DEFAULT_DDL_PATH
) -> None:
    """Apply the DDL script to the database, creating all 36 tables.

    Intended to run once against an empty database file. Deliberately NOT
    idempotent: re-running against an already-initialized database raises
    `sqlite3.OperationalError` (SQLite refuses to re-create existing
    tables). Silently swallowing that would hide real problems — e.g.
    accidentally pointing this at the wrong file, or calling it twice by
    mistake — so it's left to surface as an error rather than caught here.
    """
    ddl_path = Path(ddl_path)
    ddl_sql = ddl_path.read_text(encoding="utf-8")

    with database.transaction() as conn:
        conn.executescript(ddl_sql)
        # The DDL already includes every migration's change, so a fresh
        # database starts at the latest version with nothing to apply.
        conn.execute(f"PRAGMA user_version = {len(list_migrations())}")


# ----------------------------------------------------------------------
# Migrations: bringing an existing database up to the current DDL
# ----------------------------------------------------------------------
#
# Each approved schema change is (1) folded into the DDL file, so new
# databases get it directly, and (2) added as `schema/migrations/NNN_*.sql`,
# so databases created before it get it applied once. SQLite's
# `PRAGMA user_version` records how many migrations a database has had; a
# database from before migrations existed reads 0.

MIGRATIONS_DIR = DEFAULT_DDL_PATH.parent / "migrations"


def list_migrations(migrations_dir: Path = MIGRATIONS_DIR) -> List[Path]:
    """Migration files in the order they apply: 001_..., 002_..., ..."""
    return sorted(migrations_dir.glob("[0-9][0-9][0-9]_*.sql"))


def get_schema_version(database: Database) -> int:
    conn = database.connect()
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def pending_migrations(
    database: Database, migrations_dir: Path = MIGRATIONS_DIR
) -> List[Path]:
    return list_migrations(migrations_dir)[get_schema_version(database):]


def migrate_schema(
    database: Database, migrations_dir: Path = MIGRATIONS_DIR
) -> List[str]:
    """Apply every pending migration, each in its own transaction together
    with its version bump, so a failed migration leaves the database at the
    last good version. Returns the names of the migrations applied."""
    applied = []
    start = get_schema_version(database)
    for version, path in enumerate(list_migrations(migrations_dir)[start:], start=start + 1):
        sql = path.read_text(encoding="utf-8")
        conn = database.connect()
        try:
            # executescript runs in autocommit mode, so the transaction is
            # opened and committed inside the script itself.
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()
        applied.append(path.name)
    return applied


def get_table_names(database: Database) -> List[str]:
    """Return the names of all user tables currently in the database.

    Excludes SQLite's own internal bookkeeping tables (anything named
    `sqlite_%`, e.g. `sqlite_sequence`). Read-only; used by tests and by
    later startup checks to confirm the schema is what's expected.
    """
    conn = database.connect()
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' "
            "ORDER BY name;"
        )
        return [row["name"] for row in cursor.fetchall()]
    finally:
        conn.close()

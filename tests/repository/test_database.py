"""
Tests for `repository.database.Database`.

Covers: connection creation, foreign-key enforcement, transaction commit/
rollback behavior, and that reopening a database file works (i.e. nothing
here depends on an in-memory connection surviving between calls).
"""

import sqlite3

import pytest

from repository.database import Database


@pytest.fixture
def db_path(tmp_path):
    """A fresh, non-existent database file path for each test."""
    return tmp_path / "test.db"


def test_connect_creates_database_file(db_path):
    assert not db_path.exists()

    database = Database(db_path)
    conn = database.connect()
    conn.close()

    assert db_path.exists()


def test_foreign_keys_enabled_by_default(db_path):
    database = Database(db_path)
    assert database.foreign_keys_enabled() is True


def test_connect_returns_row_factory_that_supports_name_access(db_path):
    database = Database(db_path)
    conn = database.connect()
    try:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO t (name) VALUES ('hello');")
        conn.commit()
        row = conn.execute("SELECT * FROM t;").fetchone()
        assert row["name"] == "hello"
    finally:
        conn.close()


def test_transaction_commits_on_success(db_path):
    database = Database(db_path)

    with database.transaction() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO t (name) VALUES ('committed');")

    # Reopen with a fresh connection to prove the write was actually
    # persisted, not just visible within the same connection.
    verify_conn = database.connect()
    try:
        row = verify_conn.execute("SELECT name FROM t;").fetchone()
        assert row["name"] == "committed"
    finally:
        verify_conn.close()


def test_transaction_rolls_back_on_exception(db_path):
    database = Database(db_path)

    with database.transaction() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
    # table now exists and is committed from the block above

    with pytest.raises(RuntimeError):
        with database.transaction() as conn:
            conn.execute("INSERT INTO t (name) VALUES ('should not persist');")
            raise RuntimeError("simulated failure mid-transaction")

    verify_conn = database.connect()
    try:
        rows = verify_conn.execute("SELECT * FROM t;").fetchall()
        assert rows == []
    finally:
        verify_conn.close()


def test_transaction_rolls_back_on_foreign_key_violation(db_path):
    database = Database(db_path)

    with database.transaction() as conn:
        conn.execute(
            "CREATE TABLE parent (id INTEGER PRIMARY KEY);"
        )
        conn.execute(
            "CREATE TABLE child ("
            "  id INTEGER PRIMARY KEY,"
            "  parent_id INTEGER NOT NULL REFERENCES parent(id)"
            ");"
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as conn:
            # No matching parent row exists — foreign-key enforcement
            # (which this Database class turns on) should reject this.
            conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999);")

    verify_conn = database.connect()
    try:
        rows = verify_conn.execute("SELECT * FROM child;").fetchall()
        assert rows == []
    finally:
        verify_conn.close()


def test_reopening_database_preserves_data(db_path):
    database = Database(db_path)

    with database.transaction() as conn:
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
        conn.execute("INSERT INTO t (name) VALUES ('persisted');")

    # Simulate the application restarting: build a brand new Database
    # instance pointed at the same file path.
    reopened_database = Database(db_path)
    conn = reopened_database.connect()
    try:
        row = conn.execute("SELECT name FROM t;").fetchone()
        assert row["name"] == "persisted"
    finally:
        conn.close()

    # Foreign-key enforcement must also still be on after reopening.
    assert reopened_database.foreign_keys_enabled() is True

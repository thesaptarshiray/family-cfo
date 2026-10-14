"""
Architecture-boundary tests.

These are static checks: they parse the source under `src/` with `ast` and
`re` and never import the code they police, so they describe the code as
written rather than as it happens to behave at runtime.

Target architecture (dependencies point downward only):

    UI / API
      -> AI / CFO orchestration      (future packages under src/)
        -> financial_engine
          -> repository
            -> SQLite

Rules enforced here:

  - only `repository` may import sqlite3, hold a connection or contain SQL;
  - `financial_engine` contains no SQL and makes no raw-connection calls;
  - layers above the Engine never import `repository` or `sqlite3`;
  - no layer imports upward;
  - repositories never write an Engine-owned (derived-cache) column, never
    aggregate in SQL, and only the Engine may import `DerivedCacheRepository`;
  - the Engine does no floating-point arithmetic.

The Engine's move behind repositories is staged, and these tests record how
far it has got:

  - three ratchets that already pass and must keep passing: the Engine has no
    SQL against the ledger (`"transaction"`) table, it never writes a
    derived-cache column directly (that is `DerivedCacheRepository`'s job
    alone), and it opens no raw connection or transaction of its own (it uses
    a `UnitOfWork`);
  - two `xfail(strict=True)` tests for what is left — one SQL statement
    against `bank_account` (the account-existence check), bridged through
    `Database.scope`. Once it is gone they become unexpected passes, which
    fails the run and forces the marker to be removed, so the refactor cannot
    be declared finished while they are still marked.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
DDL_PATH = ROOT / "schema" / "family-finance-app-v6-ddl.sql"

REPOSITORY = "repository"
ENGINE = "financial_engine"

# The (table, column) pairs the V6 DDL marks "ENGINE-DERIVED CACHE". Hardcoded
# on purpose, like EXPECTED_TABLES in test_schema.py: if the DDL adds or drops
# a derived column without this list being reviewed, a test fails loudly.
EXPECTED_DERIVED_COLUMNS = {
    ("asset", "current_value"),
    ("bank_account", "current_balance"),
    ("liability", "outstanding_amount"),
    ("loan_detail", "interest_rate"),
    ("mf_holding", "avg_nav"),
    ("mf_holding", "units"),
    ("property_holding", "total_acquisition_cost"),
    ("stock_holding", "avg_buy_price"),
    ("stock_holding", "quantity"),
}

# Method/attribute names that only make sense on a raw sqlite3 connection or
# cursor. The Engine reaches persistence through repositories and a
# UnitOfWork, so none of these may appear in it. `scope` is
# `Database.scope`, the repository-internal way to get the unit's raw
# connection: the Engine uses it today only to bridge the bank_account SQL
# that has not moved yet.
RAW_CONNECTION_ATTRS = {
    "connect",
    "transaction",
    "execute",
    "executemany",
    "executescript",
    "cursor",
    "commit",
    "rollback",
    "_conn",
    "scope",
}

# The subset that is already gone from the Engine: opening a connection or a
# transaction of its own, or driving commit/rollback. The Engine's
# transaction boundary is `Database.unit_of_work()` and nothing else.
TRANSACTION_CONTROL_ATTRS = {"connect", "transaction", "commit", "rollback", "cursor", "_conn"}

_SQL_LITERAL_RE = re.compile(
    r"^\s*(SELECT\b.+?\bFROM\b|INSERT\s+(OR\s+\w+\s+)?INTO\b|"
    r"UPDATE\s+\S+\s+SET\b|DELETE\s+FROM\b|PRAGMA\b)",
    re.IGNORECASE | re.DOTALL,
)
_INSERT_RE = re.compile(
    r'INSERT\s+(?:OR\s+\w+\s+)?INTO\s+"?(\w+)"?\s*\(([^)]*)\)', re.IGNORECASE
)
_UPDATE_RE = re.compile(
    r'UPDATE\s+"?(\w+)"?\s+SET\s+(.*?)(?:\bWHERE\b|$)', re.IGNORECASE | re.DOTALL
)
_AGGREGATE_RE = re.compile(r"\b(SUM|AVG|TOTAL)\s*\(", re.IGNORECASE)
_TRANSACTION_TABLE_RE = re.compile(
    r'\b(?:FROM|INTO|UPDATE|JOIN)\s+"?transaction"?(?=[\s(;]|$)', re.IGNORECASE
)


# ----------------------------------------------------------------------
# Source scanning helpers
# ----------------------------------------------------------------------


def _packages() -> Dict[str, Path]:
    """Every top-level package under src/ (egg-info folders have no __init__)."""
    return {
        entry.name: entry
        for entry in sorted(SRC.iterdir())
        if entry.is_dir() and (entry / "__init__.py").exists()
    }


def _py_files(package: str) -> List[Path]:
    return sorted(_packages()[package].rglob("*.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _where(path: Path, lineno: int) -> str:
    return f"{path.relative_to(ROOT).as_posix()}:{lineno}"


def _imports(path: Path, tree: ast.AST) -> Iterator[Tuple[int, str, Tuple[str, ...]]]:
    """Yield (lineno, absolute dotted module, imported names) for every import.

    Covers imports anywhere in the file (including inside functions), relative
    imports resolved against the file's own package, and literal
    `__import__("x")` / `importlib.import_module("x")` calls.
    """
    package = list(path.relative_to(SRC).with_suffix("").parts[:-1])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name, ()
        elif isinstance(node, ast.ImportFrom):
            base: List[str] = []
            if node.level:
                keep = len(package) - (node.level - 1)
                if keep < 0:
                    continue
                base = package[:keep]
            tail = node.module.split(".") if node.module else []
            yield node.lineno, ".".join(base + tail), tuple(a.name for a in node.names)
        elif (
            isinstance(node, ast.Call)
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else None
            )
            if name in ("__import__", "import_module"):
                yield node.lineno, node.args[0].value, ()


def _top_level(module: str) -> str:
    return module.split(".")[0]


def _docstring_ids(tree: ast.AST) -> Set[int]:
    ids: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            first = node.body[0] if node.body else None
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _string_literals(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    """Yield every string literal that is not a docstring."""
    skip = _docstring_ids(tree)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in skip
        ):
            yield node.lineno, node.value


def _sql_literals(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    for lineno, text in _string_literals(tree):
        if _SQL_LITERAL_RE.match(text):
            yield lineno, text


def _raw_connection_uses(tree: ast.AST) -> Iterator[Tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in RAW_CONNECTION_ATTRS:
            yield node.lineno, node.attr


def _derived_columns_from_ddl() -> Set[Tuple[str, str]]:
    """(table, column) pairs on lines the DDL marks `ENGINE-DERIVED CACHE`."""
    pairs: Set[Tuple[str, str]] = set()
    table = None
    for line in DDL_PATH.read_text(encoding="utf-8").splitlines():
        created = re.match(r'\s*CREATE TABLE\s+"?(\w+)"?\s*\(', line)
        if created:
            table = created.group(1).lower()
            continue
        if table and "ENGINE-DERIVED CACHE" in line:
            column = re.match(r"\s*(\w+)\s", line)
            if column:
                pairs.add((table, column.group(1).lower()))
    return pairs


def _derived_writes(sql: str, derived: Set[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """(table, column) pairs that an INSERT/UPDATE in `sql` writes and are derived."""
    hits: List[Tuple[str, str]] = []
    for match in _INSERT_RE.finditer(sql):
        table = match.group(1).lower()
        columns = {c.strip().strip('"').lower() for c in match.group(2).split(",")}
        hits += [(table, c) for c in sorted(columns) if (table, c) in derived]
    for match in _UPDATE_RE.finditer(sql):
        table = match.group(1).lower()
        columns = {c.lower() for c in re.findall(r"(\w+)\s*=", match.group(2))}
        hits += [(table, c) for c in sorted(columns) if (table, c) in derived]
    return hits


# ----------------------------------------------------------------------
# Sanity: the scanners must actually see what they claim to police
# ----------------------------------------------------------------------


def test_scanner_sees_the_layers_it_polices():
    packages = _packages()

    assert REPOSITORY in packages
    assert ENGINE in packages
    assert _py_files(REPOSITORY), "no repository source files found"
    assert _py_files(ENGINE), "no financial_engine source files found"


def test_derived_columns_parsed_from_ddl_match_reviewed_list():
    assert _derived_columns_from_ddl() == EXPECTED_DERIVED_COLUMNS


def test_detectors_recognise_known_bad_and_known_good_samples():
    """A detector that silently matches nothing would make every rule above
    pass for the wrong reason, so pin each one against fixed samples."""
    derived = EXPECTED_DERIVED_COLUMNS

    # derived-column writes
    assert _derived_writes("UPDATE bank_account SET current_balance = ? WHERE account_id = ?", derived) == [("bank_account", "current_balance")]
    assert _derived_writes("INSERT INTO mf_holding (asset_id, units) VALUES (?, ?)", derived) == [("mf_holding", "units")]
    assert _derived_writes("UPDATE fd_holding SET interest_rate = ? WHERE asset_id = ?", derived) == []
    assert _derived_writes("SELECT current_balance FROM bank_account", derived) == []
    assert _derived_writes("INSERT INTO bank_account (member_id, bank_name) VALUES (?, ?)", derived) == []

    # SQL-shaped literals
    for sql in (
        "SELECT a FROM t",
        "INSERT INTO t (a) VALUES (?)",
        "UPDATE t SET a = ?",
        "DELETE FROM t WHERE a = ?",
        "PRAGMA foreign_keys = ON;",
    ):
        assert _SQL_LITERAL_RE.match(sql), sql
    for prose in (
        "source and destination accounts must differ",
        "Update the balance from the posted ledger rows",
        "TRANSFER",
    ):
        assert not _SQL_LITERAL_RE.match(prose), prose

    # raw connection calls, and docstrings ignored as SQL text
    tree = ast.parse("def f(db):\n    conn = db.connect()\n    conn.execute('x')\n")
    assert {attr for _, attr in _raw_connection_uses(tree)} == {"connect", "execute"}
    tree = ast.parse('def f():\n    """SELECT a FROM t"""\n    return "SELECT b FROM u"\n')
    assert [text for _, text in _sql_literals(tree)] == ["SELECT b FROM u"]

    # aggregates
    assert _AGGREGATE_RE.search("SELECT SUM(amount) FROM t")
    assert _AGGREGATE_RE.search("select avg (amount) from t")
    assert not _AGGREGATE_RE.search("SELECT amount FROM t")


# ----------------------------------------------------------------------
# Import boundaries
# ----------------------------------------------------------------------


def test_sqlite3_is_only_imported_inside_repository():
    offenders = []
    for package in _packages():
        if package == REPOSITORY:
            continue
        for path in _py_files(package):
            for lineno, module, _ in _imports(path, _parse(path)):
                if _top_level(module) == "sqlite3":
                    offenders.append(_where(path, lineno))

    assert offenders == [], f"sqlite3 imported outside the repository layer: {offenders}"


def test_layers_above_the_engine_do_not_import_repository_or_sqlite():
    """Any package other than `repository` and `financial_engine` is an upper
    layer (AI/CFO orchestration, API, UI). None exist yet, so this passes
    vacuously today and starts policing them the moment one is added."""
    forbidden = {REPOSITORY, "sqlite3"}
    offenders = []
    for package in _packages():
        if package in (REPOSITORY, ENGINE):
            continue
        for path in _py_files(package):
            for lineno, module, _ in _imports(path, _parse(path)):
                if _top_level(module) in forbidden:
                    offenders.append(f"{_where(path, lineno)} imports {module}")

    assert offenders == [], f"upper layers bypass the Engine: {offenders}"


def test_dependency_direction_is_downward_only():
    local = set(_packages())
    allowed = {
        REPOSITORY: {REPOSITORY},
        ENGINE: {REPOSITORY, ENGINE},
    }
    offenders = []
    for package, may_import in allowed.items():
        for path in _py_files(package):
            for lineno, module, _ in _imports(path, _parse(path)):
                top = _top_level(module)
                if top in local and top not in may_import:
                    offenders.append(f"{_where(path, lineno)} imports {module}")

    assert offenders == [], f"upward or sideways imports: {offenders}"


def test_derived_cache_repository_is_only_imported_by_the_engine():
    """DerivedCacheRepository may be defined and re-exported inside
    `repository` and used by `financial_engine`; nothing else may import it,
    so upper layers cannot write cache columns."""
    offenders = []
    for package in _packages():
        if package in (REPOSITORY, ENGINE):
            continue
        for path in _py_files(package):
            for lineno, module, names in _imports(path, _parse(path)):
                if "DerivedCacheRepository" in names or module.endswith("derived_cache"):
                    offenders.append(_where(path, lineno))

    assert offenders == [], f"DerivedCacheRepository imported outside the Engine: {offenders}"


# ----------------------------------------------------------------------
# Repository rules: persistence mechanics only
# ----------------------------------------------------------------------


def test_repository_never_writes_derived_columns():
    """No INSERT/UPDATE in repository SQL may touch an Engine-owned cache
    column. The single planned exception is the future `derived_cache.py`
    module, which persists complete values supplied by the Engine. Keyed by
    (table, column), so user-entered `fd_holding.interest_rate` is not flagged
    just because `loan_detail.interest_rate` is derived."""
    derived = _derived_columns_from_ddl()
    offenders = []
    for path in _py_files(REPOSITORY):
        if path.name == "derived_cache.py":
            continue
        for lineno, text in _string_literals(_parse(path)):
            for table, column in _derived_writes(text, derived):
                offenders.append(f"{_where(path, lineno)} writes {table}.{column}")

    assert offenders == [], f"repository writes derived columns: {offenders}"


def test_repository_sql_has_no_aggregates():
    """Summing and averaging are financial calculation; the repository returns
    source rows and the Engine aggregates them in Decimal."""
    offenders = []
    for path in _py_files(REPOSITORY):
        for lineno, text in _string_literals(_parse(path)):
            if _AGGREGATE_RE.search(text):
                offenders.append(_where(path, lineno))

    assert offenders == [], f"aggregate SQL in the repository layer: {offenders}"


# ----------------------------------------------------------------------
# Engine rules: no persistence mechanics, no floats
# ----------------------------------------------------------------------


def test_engine_has_no_float_arithmetic():
    offenders = []
    for path in _py_files(ENGINE):
        for node in ast.walk(_parse(path)):
            is_float_name = isinstance(node, ast.Name) and node.id == "float"
            is_float_literal = isinstance(node, ast.Constant) and isinstance(node.value, float)
            if is_float_name or is_float_literal:
                offenders.append(_where(path, node.lineno))

    assert offenders == [], f"float used in the Engine: {offenders}"


def test_engine_has_no_sql_against_the_transaction_table():
    """Ratchet: ledger persistence now lives in `TransactionRepository`. Any
    SQL naming the `"transaction"` table reappearing in the Engine fails here."""
    offenders = []
    for path in _py_files(ENGINE):
        for lineno, text in _sql_literals(_parse(path)):
            if _TRANSACTION_TABLE_RE.search(text):
                offenders.append(_where(path, lineno))

    assert offenders == [], f"Engine issues SQL against the ledger table: {offenders}"


def test_engine_never_writes_a_derived_cache_column_directly():
    """Ratchet: a derived value may only reach its column through
    `DerivedCacheRepository.set_*`. Any UPDATE/INSERT naming a derived
    (table, column) pair appearing in the Engine's own SQL fails here — that
    would be the Engine persisting a value itself instead of handing it to
    the one repository whose job that is."""
    derived = _derived_columns_from_ddl()
    offenders = []
    for path in _py_files(ENGINE):
        for lineno, text in _sql_literals(_parse(path)):
            for table, column in _derived_writes(text, derived):
                offenders.append(f"{_where(path, lineno)} writes {table}.{column}")

    assert offenders == [], f"Engine writes a derived-cache column directly: {offenders}"


def test_engine_opens_no_raw_connections_or_transactions():
    """Ratchet: the Engine's transaction boundary is `Database.unit_of_work()`.
    It no longer opens a connection or a transaction of its own, nor commits
    or rolls back."""
    offenders = []
    for path in _py_files(ENGINE):
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Attribute) and node.attr in TRANSACTION_CONTROL_ATTRS:
                offenders.append(f"{_where(path, node.lineno)} .{node.attr}")

    assert offenders == [], f"raw connection/transaction control in the Engine: {offenders}"


def test_transaction_table_detector_recognises_known_samples():
    for sql in (
        'SELECT amount FROM "transaction" WHERE account_id = ?',
        'INSERT INTO "transaction" (account_id) VALUES (?)',
        "UPDATE \"transaction\" SET status = 'REVERSED' WHERE txn_id = ?",
        "select * from transaction where txn_id = 1",
    ):
        assert _TRANSACTION_TABLE_RE.search(sql), sql
    for sql in (
        "UPDATE bank_account SET current_balance = ? WHERE account_id = ?",
        "SELECT opening_balance FROM bank_account WHERE account_id = ?",
        "INSERT INTO account_ownership (account_id) VALUES (?)",
        "Reversal of transaction",
    ):
        assert not _TRANSACTION_TABLE_RE.search(sql), sql


@pytest.mark.xfail(
    strict=True,
    reason="Engine still runs one SQL statement against bank_account (the "
    "account-existence check) through Database.scope; removed by plan Step 7.",
)
def test_engine_makes_no_raw_connection_calls():
    offenders = []
    for path in _py_files(ENGINE):
        for lineno, attr in _raw_connection_uses(_parse(path)):
            offenders.append(f"{_where(path, lineno)} .{attr}")

    assert offenders == [], f"raw connection use in the Engine: {offenders}"


@pytest.mark.xfail(
    strict=True,
    reason="Engine still contains one SQL literal, the bank_account "
    "account-existence check; removed by plan Step 7.",
)
def test_engine_contains_no_sql_literals():
    offenders = []
    for path in _py_files(ENGINE):
        for lineno, _ in _sql_literals(_parse(path)):
            offenders.append(_where(path, lineno))

    assert offenders == [], f"SQL in the Engine: {offenders}"

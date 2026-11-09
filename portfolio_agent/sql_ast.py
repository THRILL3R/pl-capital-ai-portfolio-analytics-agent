"""
SQLGlot-based AST validation: an additional, independent safety layer for read-only SQL.

Where it sits
-------------
``validate_read_only_sql`` (database.py) first applies its lexical checks (single statement, no comments,
SELECT/WITH start, forbidden keywords). This module then parses the statement with SQLGlot's SQLite dialect and
inspects the syntax tree, which catches things text matching cannot: a table-valued function such as
``pragma_table_info(...)``, a read of ``sqlite_master``, ``load_extension()``, a write hidden in a CTE or
subquery, or a statement the parser cannot understand at all. It does not replace anything:
``PortfolioDatabase.execute_read_only`` still runs the result on a ``mode=ro`` connection with
``PRAGMA query_only`` and the SQLite authorizer.

Policy (fail closed: if the statement cannot be parsed, it is rejected)
-----------------------------------------------------------------------
* exactly one statement, whose root is a SELECT or a set operation (UNION / INTERSECT / EXCEPT); a
  ``WITH`` clause is part of that SELECT in the tree, so CTE queries are allowed;
* no DML, DDL, PRAGMA, ATTACH/DETACH, transaction or other administrative node anywhere in the tree
  (including inside CTEs and subqueries);
* every table reference is one of the authorised tables or a CTE defined in the same statement, with no
  schema qualifier other than ``main``; table-valued functions are not allowed;
* functions that read or write files, load code or allocate unbounded memory are not allowed;
* only positional ``?`` placeholders (no ``:name``, ``@name``, ``$name`` or ``?NNN``); the generator's own
  parameter checks run first so their more specific error categories are kept.

Column existence is intentionally not checked here: SQLite itself rejects unknown columns, as before.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from .database import mask_sql_literals  # database imports this module lazily, so there is no import cycle

try:  # SQLGlot is a required, pinned dependency. If it cannot be imported, validate_ast() raises and the caller
    # (database._validate_ast) rejects every query: there is no way to run SQL without this layer.
    import sqlglot
    from sqlglot import exp
    from sqlglot.errors import SqlglotError
except ImportError:  # pragma: no cover - exercised via a mocked import in the tests
    sqlglot = None  # type: ignore[assignment]
    exp = None  # type: ignore[assignment]
    SqlglotError = Exception  # type: ignore[assignment,misc]

MAX_SQL_CHARS = 20_000

# Expression classes that must never appear anywhere in a read-only query (looked up by name so that
# a SQLGlot release that renames or drops one of them cannot break validation).
_FORBIDDEN_NODE_NAMES = (
    "Insert", "Update", "Delete", "Merge", "Drop", "Alter", "AlterColumn", "AlterRename", "Create",
    "TruncateTable", "Pragma", "Attach", "Detach", "Command", "Transaction", "Commit", "Rollback",
    "Set", "SetItem", "Use", "Analyze", "Copy", "LoadData", "Into", "Grant", "Revoke", "Describe",
    "Kill", "Cache", "Uncache", "Refresh", "Parameter", "SessionParameter", "Variable",
)

# Functions that touch the file system, load native code or can allocate unbounded memory.
_FORBIDDEN_FUNCTIONS = frozenset({
    "load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zeroblob", "randomblob",
    "sqlite_compileoption_used", "sqlite_compileoption_get", "sqlite_source_id",
})
_FORBIDDEN_FUNCTION_PREFIXES = ("pragma_", "sqlite_dbpage", "dbstat")


_CTE_NAME_RE = re.compile(r'\s*(?:""|[A-Za-z_]\w*)')
_CTE_AS_RE = re.compile(r"\s*AS\b(?:\s+(?:NOT\s+)?MATERIALIZED\b)?\s*", re.IGNORECASE)


def _skip_balanced(text: str, pos: int) -> int:
    """Index just after the ')' matching the '(' at ``text[pos]``, or -1 when unbalanced."""
    depth = 0
    for i in range(pos, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def check_cte_structure(sql: str) -> Optional[str]:
    """
    Structural check for statements that start with WITH. Returns a short description of the
    problem, or None when the common-table-expression list is well formed and is followed by the
    final SELECT.

    This is a *shape* check on the masked text (string literals removed), not a SQL parser:
    ``WITH name [(cols)] AS [MATERIALIZED] ( ... ) [, name AS ( ... )]*`` and then ``SELECT``.
    It exists because the model occasionally drops or mis-places the final SELECT keyword after a
    long CTE list (``... ) t.col, ... FROM x t JOIN y``), which SQLite reports only as
    ``near "t": syntax error``. It never loosens any safety rule; the statement still has to pass
    validate_read_only_sql and the database layer.
    """
    masked = mask_sql_literals(sql).strip()
    head = re.match(r"WITH\b(?:\s+RECURSIVE\b)?", masked, re.IGNORECASE)
    if not head:
        return None
    pos = head.end()
    while True:
        name = _CTE_NAME_RE.match(masked, pos)
        if not name:
            return "a CTE name was expected after WITH or ','"
        pos = name.end()
        while pos < len(masked) and masked[pos].isspace():
            pos += 1
        if pos < len(masked) and masked[pos] == "(":  # optional column list: name (a, b) AS (...)
            pos = _skip_balanced(masked, pos)
            if pos < 0:
                return "unbalanced parentheses in a CTE column list"
        as_match = _CTE_AS_RE.match(masked, pos)
        if not as_match:
            return "each CTE must be written 'name AS ( SELECT ... )'"
        pos = as_match.end()
        if pos >= len(masked) or masked[pos] != "(":
            return "each CTE body must be wrapped in parentheses"
        pos = _skip_balanced(masked, pos)
        if pos < 0:
            return "unbalanced parentheses in a CTE body"
        while pos < len(masked) and masked[pos].isspace():
            pos += 1
        if pos < len(masked) and masked[pos] == ",":
            pos += 1
            continue
        break
    follow = re.match(r"(SELECT|VALUES)\b", masked[pos:], re.IGNORECASE)
    if not follow:
        found = masked[pos:pos + 30].split()
        return (
            "the keyword SELECT must follow the last CTE's closing parenthesis, but the statement continues with "
            f"'{found[0] if found else 'nothing'}'"
        )
    return None


class SqlAstError(ValueError):
    """The statement is rejected by AST validation. The message is safe to show to the user."""


class SqlAstSyntaxError(SqlAstError):
    """The statement is not valid SQLite syntax (SQLGlot cannot parse it), as opposed to being unsafe."""


def is_available() -> bool:
    return sqlglot is not None


def _node_classes(names: Iterable[str]) -> tuple:
    return tuple(c for c in (getattr(exp, n, None) for n in names) if isinstance(c, type))


def validate_ast(sql: str, allowed_tables: Iterable[str]) -> None:
    """
    Raise SqlAstError unless ``sql`` is one read-only SELECT / set-operation statement that only reads
    ``allowed_tables`` (plus its own CTEs). Returns None when the statement is acceptable.
    """
    if sqlglot is None:
        raise SqlAstError("SQLGlot is required for AST validation but is not available.")
    if not isinstance(sql, str) or not sql.strip():
        raise SqlAstError("Empty SQL statement.")
    if len(sql) > MAX_SQL_CHARS:
        raise SqlAstError(f"SQL statement is too long (maximum {MAX_SQL_CHARS} characters).")

    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except SqlglotError as exc:
        first_line = str(exc).strip().splitlines()[0][:160] if str(exc).strip() else "syntax error"
        raise SqlAstSyntaxError(f"SQL could not be parsed as valid SQLite ({first_line}).") from exc
    except RecursionError as exc:  # pathological nesting
        raise SqlAstError("SQL is nested too deeply.") from exc

    if len(statements) != 1:
        raise SqlAstError("Exactly one SQL statement is permitted.")
    root = statements[0]

    # SQLGlot is more lenient than SQLite about some CTE shapes (e.g. a missing SELECT after the CTE list),
    # so the explicit shape check is applied as well.
    shape_problem = check_cte_structure(sql)
    if shape_problem:
        raise SqlAstSyntaxError(f"SQL could not be parsed as valid SQLite (malformed CTE list: {shape_problem}).")

    allowed_roots = _node_classes(("Select", "Union", "Intersect", "Except"))
    if not isinstance(root, allowed_roots):
        raise SqlAstError(f"Only SELECT queries are permitted (found {type(root).__name__}).")

    forbidden_nodes = _node_classes(_FORBIDDEN_NODE_NAMES)
    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE) if cte.alias_or_name}
    allowed = {t.lower() for t in allowed_tables}

    for node in root.walk():
        if isinstance(node, forbidden_nodes):
            raise SqlAstError(f"{type(node).__name__} operations are not permitted in a read-only query.")

        if isinstance(node, exp.Placeholder) and node.this not in (None, ""):
            raise SqlAstError("Only positional '?' placeholders are permitted.")

        if isinstance(node, exp.Table):
            _check_table(node, allowed, cte_names)

        if isinstance(node, exp.Func):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _FORBIDDEN_FUNCTIONS or name.startswith(_FORBIDDEN_FUNCTION_PREFIXES):
                raise SqlAstError(f"Function '{name}' is not permitted in a read-only query.")


def _check_table(node: "exp.Table", allowed: set, cte_names: set) -> None:
    if not isinstance(node.this, exp.Identifier):  # table-valued function, e.g. pragma_table_info(...)
        raise SqlAstError("Table-valued functions are not permitted.")
    name = node.name.lower()
    qualifier: Optional[str] = node.db.lower() if node.db else None
    if node.catalog or (qualifier not in (None, "main")):
        raise SqlAstError(f"Schema-qualified table '{node.sql()}' is not permitted.")
    if qualifier is None and name in cte_names:
        return
    if name not in allowed:
        raise SqlAstError(f"Table '{node.name}' is not an authorised table.")

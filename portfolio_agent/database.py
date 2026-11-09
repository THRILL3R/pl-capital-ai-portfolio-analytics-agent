"""
Database management and safe read-only SQL execution for PL Capital AI Portfolio Analytics Agent.
"""

from __future__ import annotations

import csv
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

# Expected table row counts from the original assessment datasets
EXPECTED_TABLE_COUNTS: Dict[str, int] = {
    "sectors": 10,
    "securities": 30,
    "benchmarks": 13,
    "portfolios": 13,
    "holdings": 95,
    "transactions": 24,
    "historical_prices": 330,
    "portfolio_performance": 44,
    "risk_metrics": 44,
}

# Total expected records across all nine tables
TOTAL_EXPECTED_RECORDS: int = sum(EXPECTED_TABLE_COUNTS.values())  # 603

# Dependency-respecting load order for foreign keys
LOAD_ORDER: List[str] = [
    "sectors",
    "securities",
    "benchmarks",
    "portfolios",
    "holdings",
    "transactions",
    "historical_prices",
    "portfolio_performance",
    "risk_metrics",
]

# Reverse dependency order for safe truncation / reset
REVERSE_LOAD_ORDER: List[str] = list(reversed(LOAD_ORDER))

# Forbidden SQL keywords for read-only queries
FORBIDDEN_SQL_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
)

_FORBIDDEN_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(FORBIDDEN_SQL_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


class ReadOnlyQueryError(Exception):
    """Raised when a query violates read-only safety rules or execution fails."""


class SqlSyntaxError(ReadOnlyQueryError):
    """Raised when a query is not valid SQLite syntax (found by AST parsing, before execution)."""


def mask_sql_literals(sql: str) -> str:
    """
    Mask string literals and double-quoted identifiers so that delimiters
    inside literals (like semicolons or comments) do not cause false positives.
    """
    # Replace single-quoted string literals ('foo' or 'it''s') with ''
    masked = re.sub(r"'(?:''|[^'])*'", "''", sql)
    # Replace double-quoted identifiers ("col") with ""
    masked = re.sub(r'"(?:""|[^"])*"', '""', masked)
    return masked


SQLGLOT_REQUIRED_MESSAGE = (
    "SQL rejected: SQLGlot is required for AST validation but is not available, so no query is run "
    "(install it with: pip install sqlglot==30.30.0)."
)


def _validate_ast(sql: str, allowed_tables: Optional[Any]) -> None:
    """
    SQLGlot AST layer (see sql_ast.py). Mandatory and fail-closed: if SQLGlot is missing or the validator itself
    fails unexpectedly, the query is rejected with ReadOnlyQueryError and never reaches SQLite.
    """
    from . import sql_ast

    if not sql_ast.is_available():
        logger.error(SQLGLOT_REQUIRED_MESSAGE)
        raise ReadOnlyQueryError(SQLGLOT_REQUIRED_MESSAGE)
    tables = EXPECTED_TABLE_COUNTS.keys() if allowed_tables is None else allowed_tables
    try:
        sql_ast.validate_ast(sql, tables)
    except sql_ast.SqlAstSyntaxError as exc:
        raise SqlSyntaxError(f"SQL rejected by AST validation: {exc}") from exc
    except sql_ast.SqlAstError as exc:
        raise ReadOnlyQueryError(f"SQL rejected by AST validation: {exc}") from exc
    except Exception as exc:  # a validator bug or an incompatible SQLGlot must never let a query through
        logger.error("AST validation failed unexpectedly: %s: %s", type(exc).__name__, exc)
        raise ReadOnlyQueryError(f"SQL rejected: AST validation could not be completed ({type(exc).__name__}).") from exc


def validate_read_only_sql(sql: str, allowed_tables: Optional[Any] = None, ast: bool = True) -> str:
    """
    Validate that an SQL string is strictly a single, read-only SELECT or WITH statement.

    Rules enforced:
    1. Removes optional markdown code block wrappers (```sql ... ```).
    2. Disallows empty queries.
    3. Disallows SQL comments (-- or /* ... */) to prevent smuggling.
    4. Enforces a single statement by disallowing internal semicolons.
    5. Strips one trailing semicolon if present.
    6. Ensures the statement begins strictly with SELECT or WITH.
    7. Rejects any forbidden modification/administration keywords.
    8. Parses the statement with SQLGlot (SQLite dialect) and validates the syntax tree: one SELECT /
       set-operation statement, no DML/DDL/PRAGMA anywhere (including CTEs and subqueries), only
       ``allowed_tables`` (default: the nine assessment tables) or the statement's own CTEs, no
       table-valued or file/extension functions, positional '?' placeholders only.

    ``ast=False`` skips step 8 only (used by the Gemini response parser, which runs its own parameter checks
    first and then calls this function again with the AST layer enabled).

    Raises:
        ReadOnlyQueryError: If any safety condition is violated (SqlSyntaxError for unparseable SQL).

    Returns:
        The cleaned, normalized SQL statement.
    """
    if not sql or not isinstance(sql, str):
        raise ReadOnlyQueryError("SQL query must be a non-empty string.")

    cleaned = sql.strip()

    # Strip markdown code blocks
    code_block_match = re.match(r"^```(?:sql)?\s*(.*?)\s*```$", cleaned, re.DOTALL | re.IGNORECASE)
    if code_block_match:
        cleaned = code_block_match.group(1).strip()

    if not cleaned:
        raise ReadOnlyQueryError("SQL query is empty.")

    # Mask literals for lexical checks
    masked = mask_sql_literals(cleaned)

    # Disallow SQL comment tokens outside literals
    if "--" in masked or "/*" in masked or "*/" in masked:
        raise ReadOnlyQueryError("SQL comments (-- or /* */) are not permitted in read-only queries.")

    # Strip one trailing semicolon
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
        masked = mask_sql_literals(cleaned)

    # Disallow internal semicolons (multiple statements)
    if ";" in masked:
        raise ReadOnlyQueryError("Multiple SQL statements are not permitted.")

    # Statement must start with SELECT or WITH
    first_token_match = re.match(r"^\s*([a-zA-Z]+)", masked)
    if not first_token_match:
        raise ReadOnlyQueryError("Invalid SQL query syntax.")

    first_token = first_token_match.group(1).upper()
    if first_token not in ("SELECT", "WITH"):
        raise ReadOnlyQueryError(
            f"Only read-only SELECT and WITH queries are permitted. Found statement starting with: '{first_token}'"
        )

    # Check for forbidden keywords outside string literals
    forbidden_match = _FORBIDDEN_KEYWORD_PATTERN.search(masked)
    if forbidden_match:
        keyword = forbidden_match.group(1).upper()
        raise ReadOnlyQueryError(f"Forbidden SQL keyword '{keyword}' detected in read-only query.")

    if ast:
        _validate_ast(cleaned, allowed_tables)

    return cleaned


def _authorizer_read_only(action_code: int, arg1: Any, arg2: Any, db_name: Any, trigger_name: Any) -> int:
    """
    SQLite authorizer callback for engine-level read-only enforcement.
    Explicitly denies write, DDL, and administrative operations.
    """
    denied_actions = {
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_DROP_TABLE,
        sqlite3.SQLITE_DROP_INDEX,
        sqlite3.SQLITE_DROP_VIEW,
        sqlite3.SQLITE_DROP_TRIGGER,
        sqlite3.SQLITE_ALTER_TABLE,
        sqlite3.SQLITE_CREATE_TABLE,
        sqlite3.SQLITE_CREATE_INDEX,
        sqlite3.SQLITE_CREATE_VIEW,
        sqlite3.SQLITE_CREATE_TRIGGER,
        sqlite3.SQLITE_ATTACH,
        sqlite3.SQLITE_DETACH,
        sqlite3.SQLITE_PRAGMA,
        getattr(sqlite3, "SQLITE_TRANSACTION", 22),
    }

    if action_code in denied_actions:
        return sqlite3.SQLITE_DENY

    return sqlite3.SQLITE_OK


class PortfolioDatabase:
    """
    Manages SQLite database connections, schema setup, CSV data ingestion,
    integrity validation, schema introspection, and guarded read-only query execution.
    """

    def __init__(self, db_path: Optional[Union[str, Path]] = None) -> None:
        """
        Initialize the database manager with an optional database path.
        If no path is provided, resolves to the default project database location.
        """
        self.db_path = self.resolve_database_path(db_path)
        self._write_conn: Optional[sqlite3.Connection] = None
        self._read_conn: Optional[sqlite3.Connection] = None

    @staticmethod
    def resolve_project_root() -> Path:
        """
        Resolve the project root directory containing database_schema.sql and data/.
        """
        # Candidate 1: Directory containing this file's parent (Exercise_PL_AI_Engineer/Exercise_PL_AI_Engineer)
        pkg_dir = Path(__file__).resolve().parent
        candidate1 = pkg_dir.parent
        if (candidate1 / "database_schema.sql").is_file():
            return candidate1

        # Candidate 2: Current working directory
        cwd = Path.cwd()
        if (cwd / "database_schema.sql").is_file():
            return cwd

        # Candidate 3: Nested directory from workspace root
        candidate3 = cwd / "Exercise_PL_AI_Engineer" / "Exercise_PL_AI_Engineer"
        if (candidate3 / "database_schema.sql").is_file():
            return candidate3

        candidate4 = cwd / "Exercise_PL_AI_Engineer"
        if (candidate4 / "database_schema.sql").is_file():
            return candidate4

        return candidate1

    @classmethod
    def resolve_database_path(cls, db_path: Optional[Union[str, Path]] = None) -> Path:
        """Resolve a database path, defaulting to database/portfolio.db under the project root."""
        if db_path is not None:
            return Path(db_path).resolve()
        return (cls.resolve_project_root() / "database" / "portfolio.db").resolve()

    @classmethod
    def resolve_schema_path(cls, schema_path: Optional[Union[str, Path]] = None) -> Path:
        """Resolve database_schema.sql path."""
        if schema_path is not None:
            resolved = Path(schema_path).resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"Schema file not found at: {resolved}")
            return resolved

        root = cls.resolve_project_root()
        default_path = root / "database_schema.sql"
        if default_path.is_file():
            return default_path
        raise FileNotFoundError(f"database_schema.sql not found in project root: {root}")

    @classmethod
    def resolve_data_dir(cls, data_dir: Optional[Union[str, Path]] = None) -> Path:
        """Resolve data/ directory containing the nine CSV datasets."""
        if data_dir is not None:
            resolved = Path(data_dir).resolve()
            if not resolved.is_dir():
                raise FileNotFoundError(f"Data directory not found at: {resolved}")
            return resolved

        root = cls.resolve_project_root()
        default_dir = root / "data"
        if default_dir.is_dir():
            return default_dir
        raise FileNotFoundError(f"data/ directory not found in project root: {root}")

    def get_read_only_connection(self) -> sqlite3.Connection:
        """
        Get or open a dedicated read-only connection.
        Enforces URI mode=ro, PRAGMA query_only = ON, and the SQLite authorizer.
        """
        if self._read_conn is not None:
            return self._read_conn

        if not self.db_path.is_file():
            raise FileNotFoundError(
                f"Database file does not exist at {self.db_path}. Please initialize the database first."
            )

        # Build file URI with mode=ro
        norm_path = self.db_path.resolve().as_posix()
        uri = f"file:{norm_path}?mode=ro"

        try:
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        except sqlite3.OperationalError:
            # Fallback if URI mode is not supported by OS environment
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.set_authorizer(_authorizer_read_only)

        self._read_conn = conn
        return self._read_conn

    def _authorised_tables(self) -> Optional[List[str]]:
        """User tables that exist in this database (for AST validation); None falls back to the nine known tables."""
        try:
            conn = self.get_read_only_connection()
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
            names = [r[0] for r in rows]
            return names or None
        except Exception:  # pragma: no cover - defensive: never let introspection weaken or break validation
            return None

    def execute_read_only(self, sql: str, params: Optional[tuple] = None) -> Dict[str, Any]:
        """
        Execute a validated read-only SQL query and return structured results.

        Parameters:
            sql: The raw natural-language-generated or rule-based SQL string.
            params: Optional tuple of query parameters for parameterised execution.

        Returns:
            A dictionary conforming to the assessment contract:
            {
                "sql": str,
                "columns": List[str],
                "rows": List[Dict[str, Any]],
                "row_count": int
            }

        Raises:
            ReadOnlyQueryError: If query fails validation or engine execution error occurs.
        """
        cleaned_sql = validate_read_only_sql(sql, self._authorised_tables())
        conn = self.get_read_only_connection()

        try:
            cursor = conn.cursor()
            if params:
                cursor.execute(cleaned_sql, params)
            else:
                cursor.execute(cleaned_sql)
            rows = cursor.fetchall()
            columns = [desc[0] for desc in cursor.description] if cursor.description else []
            result_rows = [dict(row) for row in rows]

            return {
                "sql": cleaned_sql,
                "columns": columns,
                "rows": result_rows,
                "row_count": len(result_rows),
            }
        except sqlite3.DatabaseError as exc:
            logger.error("Read-only query execution failed: %s | Query: %s", exc, cleaned_sql)
            raise ReadOnlyQueryError(f"Database error executing query: {exc}") from exc
        except Exception as exc:
            logger.error("Unexpected error during query execution: %s", exc)
            raise ReadOnlyQueryError(f"Query execution failed: {exc}") from exc

    def table_schema(self) -> str:
        """
        Introspect the SQLite database and generate a clean textual description
        of user tables, columns, primary keys, and foreign keys.
        Uses a dedicated connection without write permissions.
        """
        if not self.db_path.is_file():
            raise FileNotFoundError(f"Database not found at: {self.db_path}")

        # Connect with mode=ro for schema introspection
        uri = f"file:{self.db_path.resolve().as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError:
            conn = sqlite3.connect(str(self.db_path))

        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        cursor = conn.cursor()

        try:
            cursor.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
            )
            tables = cursor.fetchall()

            if not tables:
                return "No user tables found in database."

            lines: List[str] = ["# Database Schema\n"]

            for tbl in tables:
                tbl_name = tbl["name"]
                lines.append(f"Table: {tbl_name}")

                # Column information
                cursor.execute(f"PRAGMA table_info({tbl_name});")
                col_rows = cursor.fetchall()
                lines.append("  Columns:")
                for col in col_rows:
                    pk_suffix = " [PRIMARY KEY]" if col["pk"] else ""
                    notnull_suffix = " NOT NULL" if col["notnull"] else ""
                    default_val = f" DEFAULT {col['dflt_value']}" if col["dflt_value"] is not None else ""
                    lines.append(f"    - {col['name']} ({col['type']}{notnull_suffix}{default_val}){pk_suffix}")

                # Foreign key information
                cursor.execute(f"PRAGMA foreign_key_list({tbl_name});")
                fk_rows = cursor.fetchall()
                if fk_rows:
                    lines.append("  Foreign Keys:")
                    for fk in fk_rows:
                        lines.append(f"    - {fk['from']} -> {fk['table']}({fk['to']})")

                lines.append("")

            return "\n".join(lines).strip()
        finally:
            conn.close()

    def is_initialized(self) -> bool:
        """Check whether the database exists and contains all expected tables with non-zero records."""
        if not self.db_path.is_file():
            return False

        try:
            conn = sqlite3.connect(str(self.db_path))
            cursor = conn.cursor()
            for tbl, expected_count in EXPECTED_TABLE_COUNTS.items():
                cursor.execute(f"SELECT COUNT(*) FROM {tbl};")
                count = cursor.fetchone()[0]
                if count != expected_count:
                    conn.close()
                    return False
            conn.close()
            return True
        except Exception:
            return False

    def verify_integrity(self) -> Dict[str, Any]:
        """
        Run complete data integrity checks on the database:
        - Row counts per table against ground-truth expectations.
        - PRAGMA foreign_key_check.
        - PRAGMA integrity_check.
        """
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        table_counts: Dict[str, int] = {}
        for tbl in LOAD_ORDER:
            cursor.execute(f"SELECT COUNT(*) FROM {tbl};")
            table_counts[tbl] = cursor.fetchone()[0]

        cursor.execute("PRAGMA foreign_key_check;")
        fk_violations = cursor.fetchall()

        cursor.execute("PRAGMA integrity_check;")
        integrity_status = cursor.fetchone()[0]

        conn.close()

        total_rows = sum(table_counts.values())
        counts_match = table_counts == EXPECTED_TABLE_COUNTS

        return {
            "table_counts": table_counts,
            "total_records": total_rows,
            "expected_total_records": TOTAL_EXPECTED_RECORDS,
            "counts_match": counts_match,
            "fk_violations_count": len(fk_violations),
            "fk_violations": [dict(v) for v in fk_violations],
            "integrity_check": integrity_status,
            "is_valid": counts_match and len(fk_violations) == 0 and integrity_status == "ok",
        }

    def initialize_database(
        self,
        schema_path: Optional[Union[str, Path]] = None,
        data_dir: Optional[Union[str, Path]] = None,
        reset: bool = False,
    ) -> Dict[str, Any]:
        """
        Initialize the SQLite database with database_schema.sql and all nine CSV datasets.

        If reset=False and the database is already fully initialized with expected record counts,
        it skips re-importing to avoid duplication.

        If reset=True or database is not initialized, it builds to an atomic staging file first
        and only replaces the destination database upon passing all integrity checks. This guarantees
        that an existing valid database is never corrupted if rebuilding fails.
        """
        resolved_schema = self.resolve_schema_path(schema_path)
        resolved_data_dir = self.resolve_data_dir(data_dir)

        # If already populated and reset is not requested, return status safely
        if not reset and self.is_initialized():
            logger.info("Database at %s is already initialized with expected records.", self.db_path)
            integrity = self.verify_integrity()
            return {
                "status": "already_initialized",
                "database_path": str(self.db_path),
                "rebuilt": False,
                "integrity": integrity,
            }

        # Close any existing open connections
        self.close()

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic build via staging file
        staging_path = self.db_path.with_suffix(".tmp")
        if staging_path.exists():
            try:
                staging_path.unlink()
            except Exception:
                pass

        logger.info("Building database at staging path: %s", staging_path)

        staging_conn: Optional[sqlite3.Connection] = None
        try:
            staging_conn = sqlite3.connect(str(staging_path))
            staging_conn.execute("PRAGMA foreign_keys = ON;")

            # 1. Execute schema DDL
            with open(resolved_schema, "r", encoding="utf-8") as schema_file:
                schema_sql = schema_file.read()
                staging_conn.executescript(schema_sql)

            # 2. Ingest CSV files in strict dependency order
            table_counts: Dict[str, int] = {}
            for table_name in LOAD_ORDER:
                csv_path = resolved_data_dir / f"{table_name}.csv"
                if not csv_path.is_file():
                    raise FileNotFoundError(f"Required CSV file missing: {csv_path}")

                count = self._load_csv_to_table(staging_conn, table_name, csv_path)
                table_counts[table_name] = count

            staging_conn.commit()

            # 3. Validate foreign key integrity
            cursor = staging_conn.cursor()
            cursor.execute("PRAGMA foreign_key_check;")
            fk_issues = cursor.fetchall()
            if fk_issues:
                raise RuntimeError(f"Foreign key integrity check failed with {len(fk_issues)} violations.")

            # 4. Validate database integrity
            cursor.execute("PRAGMA integrity_check;")
            integrity_result = cursor.fetchone()[0]
            if integrity_result != "ok":
                raise RuntimeError(f"PRAGMA integrity_check returned: {integrity_result}")

            # 5. Validate expected record counts
            for tbl, expected in EXPECTED_TABLE_COUNTS.items():
                actual = table_counts.get(tbl, 0)
                if actual != expected:
                    raise ValueError(
                        f"Record count mismatch for table '{tbl}': expected {expected}, got {actual}"
                    )

            staging_conn.close()
            staging_conn = None

            # 6. Atomic swap: replace destination with staging file
            if os.name == "nt" and self.db_path.exists():
                # On Windows, replace can occasionally raise if another handle is lingering
                try:
                    self.db_path.unlink()
                except Exception:
                    pass

            os.replace(staging_path, self.db_path)
            logger.info("Database successfully built and moved to: %s", self.db_path)

            return {
                "status": "success",
                "database_path": str(self.db_path),
                "rebuilt": True,
                "table_counts": table_counts,
                "total_records": sum(table_counts.values()),
                "fk_violations": 0,
                "integrity": "ok",
            }

        except Exception as exc:
            logger.error("Database initialization failed: %s. Cleaning up staging file.", exc)
            # Close the staging connection first: on Windows an open handle locks the
            # file and the unlink below would silently fail.
            if staging_conn is not None:
                try:
                    staging_conn.close()
                except Exception:
                    pass
            if staging_path.exists():
                try:
                    staging_path.unlink()
                except Exception:
                    pass
            raise

    def _load_csv_to_table(self, conn: sqlite3.Connection, table_name: str, csv_path: Path) -> int:
        """
        Load a CSV file into a database table using parameterized inserts.
        Validates CSV headers against table columns, handles UTF-8 BOM, and converts empty strings to None.
        """
        # Introspect table columns
        cursor = conn.cursor()
        cursor.execute(f"PRAGMA table_info({table_name});")
        table_cols_info = cursor.fetchall()
        table_col_names = {row[1] for row in table_cols_info}

        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as csv_file:
            reader = csv.DictReader(csv_file)
            csv_headers = reader.fieldnames or []

            # Header validation
            unknown_cols = set(csv_headers) - table_col_names
            if unknown_cols:
                raise ValueError(
                    f"CSV for table '{table_name}' contains unknown columns not in schema: {unknown_cols}"
                )

            # Check that NOT NULL columns without default values are present in CSV
            for col_info in table_cols_info:
                cid, col_name, col_type, not_null, default_val, is_pk = col_info
                if not_null and default_val is None and not is_pk and col_name not in csv_headers:
                    raise ValueError(
                        f"CSV for table '{table_name}' is missing mandatory NOT NULL column: '{col_name}'"
                    )

            # Prepare parameterized INSERT
            cols_clause = ", ".join(f'"{h}"' for h in csv_headers)
            placeholders = ", ".join("?" for _ in csv_headers)
            insert_sql = f"INSERT INTO {table_name} ({cols_clause}) VALUES ({placeholders});"

            rows_to_insert: List[List[Any]] = []
            for row_dict in reader:
                # Convert empty string to None so SQLite inserts NULL
                row_vals = [None if row_dict[h] == "" else row_dict[h] for h in csv_headers]
                rows_to_insert.append(row_vals)

            cursor.executemany(insert_sql, rows_to_insert)

        return len(rows_to_insert)

    def close(self) -> None:
        """Close any open database connections."""
        if self._read_conn is not None:
            try:
                self._read_conn.close()
            except Exception:
                pass
            self._read_conn = None

        if self._write_conn is not None:
            try:
                self._write_conn.close()
            except Exception:
                pass
            self._write_conn = None

    def __enter__(self) -> PortfolioDatabase:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

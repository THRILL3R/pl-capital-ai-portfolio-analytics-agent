"""
Comprehensive unit tests for PortfolioDatabase.
Covers initialization, data integrity, dynamic schema extraction, and read-only SQL enforcement.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
import sys
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.database import (
    PortfolioDatabase,
    ReadOnlyQueryError,
    validate_read_only_sql,
    EXPECTED_TABLE_COUNTS,
    TOTAL_EXPECTED_RECORDS,
    LOAD_ORDER,
)


class TestPortfolioDatabase(unittest.TestCase):
    """Test suite for PortfolioDatabase operations and read-only query safety."""

    @classmethod
    def setUpClass(cls):
        """Set up a temporary directory and build a test database once for read tests."""
        cls.temp_dir = tempfile.mkdtemp(prefix="pl_capital_test_")
        cls.db_path = Path(cls.temp_dir) / "test_portfolio.db"
        cls.schema_path = PortfolioDatabase.resolve_schema_path()
        cls.data_dir = PortfolioDatabase.resolve_data_dir()

        cls.db = PortfolioDatabase(cls.db_path)
        cls.init_result = cls.db.initialize_database(
            schema_path=cls.schema_path,
            data_dir=cls.data_dir,
            reset=True,
        )

    @classmethod
    def tearDownClass(cls):
        """Clean up temporary directory and close connections."""
        cls.db.close()
        try:
            shutil.rmtree(cls.temp_dir)
        except Exception:
            pass

    def test_01_successful_database_initialization(self):
        """Verify successful database initialization and file existence."""
        self.assertTrue(self.db_path.is_file(), "Database file must exist after initialization.")
        self.assertEqual(self.init_result.get("status"), "success")
        self.assertTrue(self.init_result.get("rebuilt"))

    def test_02_all_nine_tables_existing(self):
        """Verify all nine tables exist in sqlite_master."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()

        for tbl in LOAD_ORDER:
            self.assertIn(tbl, tables, f"Expected table '{tbl}' was not found in the database.")
        self.assertEqual(len(tables), 9, "Exactly 9 user tables must exist in the database.")

    def test_03_all_603_expected_records(self):
        """Verify that every table contains its exact expected record count, summing to 603."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        total_records = 0

        for tbl, expected_count in EXPECTED_TABLE_COUNTS.items():
            cursor.execute(f"SELECT COUNT(*) FROM {tbl};")
            actual_count = cursor.fetchone()[0]
            self.assertEqual(
                actual_count,
                expected_count,
                f"Table '{tbl}' count mismatch: expected {expected_count}, got {actual_count}.",
            )
            total_records += actual_count

        conn.close()
        self.assertEqual(total_records, TOTAL_EXPECTED_RECORDS)
        self.assertEqual(total_records, 603)

    def test_04_zero_foreign_key_violations(self):
        """Verify that PRAGMA foreign_key_check returns zero violations."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_key_check;")
        violations = cursor.fetchall()
        conn.close()

        self.assertEqual(len(violations), 0, f"Foreign key violations found: {violations}")

    def test_05_database_integrity(self):
        """Verify that PRAGMA integrity_check returns 'ok'."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()
        cursor.execute("PRAGMA integrity_check;")
        status = cursor.fetchone()[0]
        conn.close()

        self.assertEqual(status, "ok", f"Integrity check failed with: {status}")

    def test_06_csv_schema_column_validation(self):
        """Verify that all CSV headers match the database schema columns."""
        conn = sqlite3.connect(str(self.db_path))
        cursor = conn.cursor()

        for tbl in LOAD_ORDER:
            cursor.execute(f"PRAGMA table_info({tbl});")
            table_cols = {row[1] for row in cursor.fetchall()}

            csv_file = self.data_dir / f"{tbl}.csv"
            import csv
            with open(csv_file, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                csv_cols = set(reader.fieldnames or [])

            # Every column in the CSV must exist in the table schema
            unknown_cols = csv_cols - table_cols
            self.assertEqual(
                len(unknown_cols),
                0,
                f"CSV for '{tbl}' has columns not in schema: {unknown_cols}",
            )

        conn.close()

    def test_07_dynamic_schema_extraction(self):
        """Verify table_schema() outputs clean, comprehensive schema metadata."""
        schema_text = self.db.table_schema()
        self.assertIsInstance(schema_text, str)
        self.assertIn("# Database Schema", schema_text)

        for tbl in LOAD_ORDER:
            self.assertIn(f"Table: {tbl}", schema_text)

        # Check key columns
        self.assertIn("portfolio_name", schema_text)
        self.assertIn("current_weight", schema_text)
        self.assertIn("sector_id", schema_text)
        self.assertIn("FOREIGN KEY", schema_text.upper())

    def test_08_successful_select_execution(self):
        """Verify executing valid SELECT queries via execute_read_only()."""
        # Simple count
        result = self.db.execute_read_only("SELECT COUNT(*) AS total_portfolios FROM portfolios;")
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["columns"], ["total_portfolios"])
        self.assertEqual(result["rows"][0]["total_portfolios"], 13)

        # Join query
        join_query = """
        SELECT s.symbol, s.company_name, sec.sector_name
        FROM securities s
        JOIN sectors sec ON s.sector_id = sec.sector_id
        WHERE sec.sector_name = 'Technology'
        ORDER BY s.symbol;
        """
        join_result = self.db.execute_read_only(join_query)
        self.assertEqual(join_result["row_count"], 8)
        self.assertIn("AAPL", [r["symbol"] for r in join_result["rows"]])

    def test_09_successful_with_query_execution(self):
        """Verify executing complex queries with Common Table Expressions (WITH / CTE)."""
        cte_query = """
        WITH high_risk AS (
            SELECT portfolio_id, portfolio_name, total_aum
            FROM portfolios
            WHERE target_risk_level = 'High'
        )
        SELECT COUNT(*) as high_risk_count, SUM(total_aum) as total_high_risk_aum
        FROM high_risk;
        """
        result = self.db.execute_read_only(cte_query)
        self.assertEqual(result["row_count"], 1)
        self.assertEqual(result["rows"][0]["high_risk_count"], 4)
        self.assertEqual(result["rows"][0]["total_high_risk_aum"], 85000000.0)

    def test_10_rejection_of_delete_and_drop(self):
        """Verify that DELETE and DROP statements are strictly rejected."""
        with self.assertRaises(ReadOnlyQueryError):
            self.db.execute_read_only("DELETE FROM portfolios WHERE portfolio_id = 1;")

        with self.assertRaises(ReadOnlyQueryError):
            self.db.execute_read_only("DROP TABLE sectors;")

    def test_11_rejection_of_multiple_statements(self):
        """Verify that multiple statements are rejected, while preserving semicolons in literals."""
        # Multi-statement injection attempt
        with self.assertRaises(ReadOnlyQueryError):
            self.db.execute_read_only("SELECT * FROM portfolios; DROP TABLE portfolios;")

        with self.assertRaises(ReadOnlyQueryError):
            self.db.execute_read_only("SELECT 1; SELECT 2;")

        # Valid query with semicolon inside string literal must be permitted
        valid_with_semicolon = "SELECT * FROM portfolios WHERE portfolio_name = 'Tech; Innovation';"
        result = self.db.execute_read_only(valid_with_semicolon)
        self.assertIsInstance(result, dict)

    def test_12_read_only_enforcement(self):
        """Verify rejection of INSERT, UPDATE, PRAGMA, comments, and non-SELECT statements."""
        unsafe_queries = [
            "INSERT INTO sectors (sector_id, sector_name) VALUES (99, 'Test');",
            "UPDATE portfolios SET total_aum = 0;",
            "ALTER TABLE portfolios ADD COLUMN test_col TEXT;",
            "CREATE TABLE hack (id INT);",
            "ATTACH DATABASE 'evil.db' AS evil;",
            "PRAGMA table_info(portfolios);",
            "SELECT * FROM portfolios -- comment",
            "SELECT * FROM portfolios /* inline comment */ WHERE 1=1",
            "EXEC xp_cmdshell('dir')",
        ]

        for unsafe in unsafe_queries:
            with self.subTest(query=unsafe):
                with self.assertRaises(ReadOnlyQueryError):
                    self.db.execute_read_only(unsafe)

    def test_13_safe_repeated_initialization(self):
        """Verify that calling initialize_database without reset on an existing DB is idempotent."""
        # Second call with reset=False
        second_result = self.db.initialize_database(
            schema_path=self.schema_path,
            data_dir=self.data_dir,
            reset=False,
        )
        self.assertEqual(second_result["status"], "already_initialized")
        self.assertFalse(second_result["rebuilt"])

        # Row counts must still be exactly 603
        verification = self.db.verify_integrity()
        self.assertEqual(verification["total_records"], 603)
        self.assertEqual(verification["fk_violations_count"], 0)

    def test_14_database_reset_behaviour(self):
        """Verify that database reset cleanly rebuilds without duplicating or corrupting records."""
        reset_result = self.db.initialize_database(
            schema_path=self.schema_path,
            data_dir=self.data_dir,
            reset=True,
        )
        self.assertEqual(reset_result["status"], "success")
        self.assertTrue(reset_result["rebuilt"])
        self.assertEqual(reset_result["total_records"], 603)
        self.assertEqual(reset_result["fk_violations"], 0)

    def test_15_atomic_rebuild_failure_safety(self):
        """Verify that if rebuilding fails, the existing valid database is preserved."""
        # Create a separate database
        test_db_path = Path(self.temp_dir) / "atomic_safety.db"
        safe_db = PortfolioDatabase(test_db_path)
        safe_db.initialize_database(self.schema_path, self.data_dir, reset=True)

        original_mtime = test_db_path.stat().st_mtime
        original_count = safe_db.verify_integrity()["total_records"]
        self.assertEqual(original_count, 603)

        # Attempt to reset with an invalid schema file
        bad_schema = Path(self.temp_dir) / "bad_schema.sql"
        bad_schema.write_text("CREATE TABLE broken (id INT PRIMARY KEY SYNTAX ERROR);")

        with self.assertRaises(Exception):
            safe_db.initialize_database(schema_path=bad_schema, data_dir=self.data_dir, reset=True)

        # The original database must still be intact with 603 rows
        intact_db = PortfolioDatabase(test_db_path)
        verification = intact_db.verify_integrity()
        self.assertTrue(verification["is_valid"])
        self.assertEqual(verification["total_records"], 603)
        intact_db.close()


if __name__ == "__main__":
    unittest.main()

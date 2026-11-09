"""
Tests for the SQLGlot AST validation layer (portfolio_agent/sql_ast.py) and its integration in
validate_read_only_sql / PortfolioDatabase.execute_read_only / GeminiSQLGenerator.

Two kinds of assertion are used on purpose:
* ``validate_ast`` directly: proves the AST layer rejects a statement on its own, independent of the
  older lexical checks (which would otherwise mask it, e.g. the DELETE keyword);
* ``validate_read_only_sql`` / ``execute_read_only``: proves the combined, public behaviour.
No Gemini call is made anywhere (the client is a mock).
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent import database as database_module
from portfolio_agent import sql_ast
from portfolio_agent.database import EXPECTED_TABLE_COUNTS, PortfolioDatabase, ReadOnlyQueryError, SqlSyntaxError, validate_read_only_sql
from portfolio_agent.sql_ast import SqlAstError, SqlAstSyntaxError, validate_ast
from portfolio_agent.sql_generator import GeminiGenerationError, GeminiSQLGenerator, RuleBasedSQLGenerator
from portfolio_agent.tools import SQLQueryTool

TABLES = list(EXPECTED_TABLE_COUNTS)

# Valid Q7-style statement as Gemini writes it after the repair (final SELECT present, two CTEs).
Q7_GEMINI_STYLE = (
    "WITH portfolio_totals AS (SELECT p.portfolio_id, p.portfolio_name, SUM(h.quantity * s.current_price) AS total_portfolio_value "
    "FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id "
    "JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id), "
    "tech_holdings AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) AS tech_market_value "
    "FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id "
    "JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id "
    "WHERE sec.sector_name = ? GROUP BY p.portfolio_id) "
    "SELECT pt.portfolio_name, t.tech_market_value, pt.total_portfolio_value, "
    "ROUND((t.tech_market_value * 100.0) / pt.total_portfolio_value, 2) AS percentage "
    "FROM tech_holdings t JOIN portfolio_totals pt ON t.portfolio_id = pt.portfolio_id ORDER BY percentage DESC"
)
Q7_MALFORMED = Q7_GEMINI_STYLE.replace(") SELECT pt.portfolio_name, t.tech_market_value,", ") t.portfolio_name, t.tech_market_value,")


def ok(sql: str) -> None:
    validate_ast(sql, TABLES)


class TestAstAcceptsLegitimateQueries(unittest.TestCase):
    VALID = {
        "count": "SELECT COUNT(*) FROM portfolios",
        "parameters": "SELECT portfolio_name FROM portfolios WHERE status = ? AND total_aum > ?",
        "join + group + order + limit": (
            "SELECT s.sector_name, COUNT(*) AS n, ROUND(AVG(sec.current_price), 2) AS avg_price FROM sectors s "
            "JOIN securities sec ON sec.sector_id = s.sector_id GROUP BY s.sector_name HAVING COUNT(*) > 1 "
            "ORDER BY n DESC LIMIT 5"
        ),
        "left join + case + cast": (
            "SELECT p.portfolio_name, CASE WHEN p.status = 'Active' THEN 1 ELSE 0 END AS active, "
            "CAST(COUNT(h.holding_id) AS REAL) / 2 AS ratio FROM portfolios p LEFT JOIN holdings h ON p.portfolio_id = h.portfolio_id GROUP BY p.portfolio_id"
        ),
        "union": "SELECT portfolio_name FROM portfolios UNION SELECT company_name FROM securities",
        "schema-qualified main": "SELECT COUNT(*) FROM main.holdings",
        "join using": "SELECT h.holding_id FROM holdings h JOIN securities s USING (security_id)",
        "single CTE": "WITH a AS (SELECT portfolio_id FROM portfolios) SELECT COUNT(*) FROM a",
        "multi CTE": (
            "WITH a AS (SELECT portfolio_id FROM portfolios), b AS (SELECT portfolio_id, COUNT(*) AS n FROM holdings GROUP BY portfolio_id), "
            "c AS (SELECT a.portfolio_id, b.n FROM a JOIN b ON a.portfolio_id = b.portfolio_id) SELECT * FROM c ORDER BY n DESC"
        ),
        "recursive CTE": "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3) SELECT i FROM n",
        "scalar subquery": "SELECT portfolio_name, (SELECT COUNT(*) FROM holdings h WHERE h.portfolio_id = p.portfolio_id) AS n FROM portfolios p",
        "IN subquery": "SELECT portfolio_name FROM portfolios WHERE portfolio_id IN (SELECT portfolio_id FROM holdings)",
        "EXISTS subquery": "SELECT portfolio_name FROM portfolios p WHERE EXISTS (SELECT 1 FROM holdings h WHERE h.portfolio_id = p.portfolio_id)",
        "derived table": "SELECT x.n FROM (SELECT COUNT(*) AS n FROM holdings) x",
        "nested derived tables": "SELECT y.n FROM (SELECT x.n FROM (SELECT COUNT(*) AS n FROM holdings) x) y",
        "string literal containing SQL words": "SELECT portfolio_name FROM portfolios WHERE portfolio_name = 'DROP TABLE holdings; --'",
        "Q7 as Gemini writes it": Q7_GEMINI_STYLE,
    }

    def test_valid_queries_pass_the_ast_layer(self):
        for name, sql in self.VALID.items():
            with self.subTest(name):
                ok(sql)

    def test_valid_queries_pass_the_combined_validator(self):
        for name, sql in self.VALID.items():
            with self.subTest(name):
                self.assertTrue(validate_read_only_sql(sql))

    def test_trailing_semicolon_is_still_accepted(self):
        self.assertEqual(validate_read_only_sql("SELECT 1 FROM portfolios;"), "SELECT 1 FROM portfolios")


class TestAstRejectsUnsafeStatements(unittest.TestCase):
    """The AST layer on its own (does not rely on the keyword filter)."""

    def test_dml_and_ddl_statements(self):
        for sql in (
            "INSERT INTO holdings (holding_id) VALUES (1)",
            "UPDATE portfolios SET status = 'Closed'",
            "DELETE FROM holdings",
            "DROP TABLE holdings",
            "ALTER TABLE holdings ADD COLUMN x INT",
            "CREATE TABLE evil (x INT)",
            "CREATE VIEW v AS SELECT 1",
            "REPLACE INTO holdings VALUES (1)",
            "PRAGMA table_info(holdings)",
            "PRAGMA writable_schema = 1",
            "ATTACH DATABASE 'other.db' AS other",
            "DETACH DATABASE other",
            "VACUUM",
            "REINDEX",
            "ANALYZE",
            "EXPLAIN SELECT 1",
            "BEGIN",
            "VALUES (1)",
        ):
            with self.subTest(sql), self.assertRaises(SqlAstError):
                validate_ast(sql, TABLES)

    def test_dml_hidden_inside_ctes_and_subqueries(self):
        for sql in (
            "WITH a AS (DELETE FROM holdings RETURNING *) SELECT * FROM a",
            "WITH a AS (UPDATE portfolios SET status = 'x' RETURNING *) SELECT * FROM a",
            "WITH a AS (INSERT INTO holdings DEFAULT VALUES RETURNING *) SELECT * FROM a",
            "SELECT * FROM (DELETE FROM holdings RETURNING *) x",
            "SELECT * FROM holdings WHERE holding_id IN (DELETE FROM holdings RETURNING holding_id)",
        ):
            with self.subTest(sql), self.assertRaises(SqlAstError):
                validate_ast(sql, TABLES)

    def test_multiple_statements(self):
        for sql in ("SELECT 1; SELECT 2", "SELECT 1 FROM portfolios; DROP TABLE holdings", "SELECT 1; PRAGMA query_only = OFF"):
            with self.subTest(sql), self.assertRaises(SqlAstError):
                validate_ast(sql, TABLES)

    def test_reads_that_the_keyword_filter_cannot_see(self):
        for sql, needle in (
            ("SELECT * FROM sqlite_master", "not an authorised table"),
            ("SELECT name FROM sqlite_schema", "not an authorised table"),
            ("SELECT * FROM sqlite_temp_master", "not an authorised table"),
            ("SELECT * FROM pragma_table_info('holdings')", "Table-valued"),
            ("SELECT * FROM json_each('[1,2]')", "Table-valued"),
            ("SELECT * FROM generate_series(1, 100000000)", "Table-valued"),
            ("SELECT load_extension('evil.dll')", "load_extension"),
            ("SELECT readfile('C:/secrets.txt')", "readfile"),
            ("SELECT writefile('x.txt', 'data')", "writefile"),
            ("SELECT zeroblob(2000000000)", "zeroblob"),
            ("SELECT randomblob(2000000000)", "randomblob"),
            ("SELECT * FROM holdings UNION SELECT name, sql, 1, 1, 1, 1 FROM sqlite_master", "not an authorised table"),
            ("SELECT * FROM temp.holdings", "Schema-qualified"),
            ("SELECT * FROM other.holdings", "Schema-qualified"),
        ):
            with self.subTest(sql), self.assertRaises(SqlAstError) as ctx:
                validate_ast(sql, TABLES)
            self.assertIn(needle, str(ctx.exception))

    def test_unauthorised_and_invalid_table_references(self):
        for sql in (
            "SELECT * FROM users",
            "SELECT * FROM holdings h JOIN secrets s ON s.id = h.holding_id",
            "WITH a AS (SELECT * FROM missing_table) SELECT * FROM a",
            "SELECT * FROM portfolios WHERE portfolio_id IN (SELECT portfolio_id FROM hidden)",
            "SELECT (SELECT 1 FROM nowhere)",
        ):
            with self.subTest(sql), self.assertRaises(SqlAstError) as ctx:
                validate_ast(sql, TABLES)
            self.assertIn("not an authorised table", str(ctx.exception))

    def test_cte_names_are_not_a_loophole(self):
        # A CTE may be referenced, but a CTE body is validated like any other query.
        with self.assertRaises(SqlAstError):
            validate_ast("WITH a AS (SELECT * FROM sqlite_master) SELECT * FROM a", TABLES)
        validate_ast("WITH a AS (SELECT 1 AS x) SELECT * FROM a", TABLES)

    def test_placeholders_must_be_positional(self):
        ok("SELECT * FROM portfolios WHERE status = ? AND portfolio_id > ?")
        for sql in ("SELECT * FROM portfolios WHERE status = :status", "SELECT * FROM portfolios WHERE status = ?1", "SELECT * FROM portfolios WHERE status = @s"):
            with self.subTest(sql), self.assertRaises(SqlAstError):
                validate_ast(sql, TABLES)

    def test_empty_and_oversized_statements(self):
        with self.assertRaises(SqlAstError):
            validate_ast("   ", TABLES)
        with self.assertRaises(SqlAstError):
            validate_ast("SELECT " + "1," * sql_ast.MAX_SQL_CHARS + "1", TABLES)

    def test_allowed_tables_are_configurable(self):
        validate_ast("SELECT * FROM custom_table", ["custom_table"])
        with self.assertRaises(SqlAstError):
            validate_ast("SELECT * FROM holdings", ["custom_table"])


class TestMalformedCtes(unittest.TestCase):
    def test_exact_gemini_malformed_q7_is_rejected_as_a_syntax_error(self):
        self.assertNotEqual(Q7_MALFORMED, Q7_GEMINI_STYLE)
        with self.assertRaises(SqlAstSyntaxError):
            validate_ast(Q7_MALFORMED, TABLES)
        with self.assertRaises(SqlSyntaxError):  # same through the public validator (a ReadOnlyQueryError subclass)
            validate_read_only_sql(Q7_MALFORMED)
        with self.assertRaises(ReadOnlyQueryError):
            validate_read_only_sql(Q7_MALFORMED)

    def test_other_malformed_ctes(self):
        for sql in (
            "WITH a AS (SELECT 1) FROM a",
            "WITH a AS (SELECT 1), b AS (SELECT 2) JOIN b ON 1 = 1 SELECT 1",
            "WITH a AS (SELECT 1) a.x FROM a",
            "WITH a AS (SELECT 1)",
            "WITH a (SELECT 1) SELECT * FROM a",
            "WITH a AS SELECT 1 SELECT * FROM a",
            "WITH a AS (SELECT 1, b AS (SELECT 2) SELECT 1",
            "SELECT FROM WHERE",
            "SELECT * FROM",
        ):
            with self.subTest(sql), self.assertRaises(SqlAstError):
                validate_ast(sql, TABLES)


class TestCombinedValidatorKeepsEveryExistingCheck(unittest.TestCase):
    def test_lexical_checks_still_fire_first_with_their_original_messages(self):
        for sql, needle in (
            ("SELECT 1; SELECT 2", "Multiple SQL statements"),
            ("SELECT 1 -- hi", "comments"),
            ("DELETE FROM holdings", "Only read-only SELECT"),
            ("SELECT 1 FROM holdings WHERE 1=1 OR (SELECT COUNT(*) FROM x) UPDATE", "UPDATE"),
            ("WITH a AS (SELECT 1) DELETE FROM holdings", "DELETE"),
            ("", "non-empty"),
        ):
            with self.subTest(sql), self.assertRaises(ReadOnlyQueryError) as ctx:
                validate_read_only_sql(sql)
            self.assertIn(needle, str(ctx.exception))

    def test_injection_attempts(self):
        attacks = (
            "SELECT * FROM holdings WHERE portfolio_id = 1 OR 1=1; DROP TABLE holdings",
            "SELECT * FROM holdings WHERE portfolio_id = 1; ATTACH DATABASE 'x.db' AS x",
            "SELECT * FROM portfolios WHERE portfolio_name = '' UNION SELECT name FROM sqlite_master --'",
            "SELECT * FROM portfolios WHERE portfolio_name = '' UNION SELECT sql FROM sqlite_master",
            "SELECT * FROM portfolios WHERE 1=1; PRAGMA query_only = OFF",
            "SELECT * FROM portfolios /* x */ WHERE 1=1",
            "SELECT * FROM portfolios WHERE portfolio_id = (SELECT 1); DELETE FROM holdings",
            "SELECT * FROM pragma_database_list",
            "SELECT * FROM portfolios WHERE portfolio_name = '' OR load_extension('x') IS NULL",
        )
        for sql in attacks:
            with self.subTest(sql), self.assertRaises(ReadOnlyQueryError):
                validate_read_only_sql(sql)

    def test_missing_sqlglot_fails_closed_even_for_a_perfectly_valid_query(self):
        with patch.object(sql_ast, "sqlglot", None):
            self.assertFalse(sql_ast.is_available())
            with self.assertLogs(database_module.logger, level=logging.ERROR) as logs:
                with self.assertRaises(ReadOnlyQueryError) as ctx:
                    validate_read_only_sql("SELECT COUNT(*) FROM portfolios")
            self.assertIn("SQLGlot is required", str(ctx.exception))
            self.assertIn("sqlglot==30.30.0", str(ctx.exception))
            self.assertIn("SQLGlot is required", logs.output[0])
            self.assertNotIsInstance(ctx.exception, SqlSyntaxError)  # a missing dependency is not a syntax problem
            with self.assertRaises(SqlAstError):
                validate_ast("SELECT 1 FROM portfolios", TABLES)

    def test_missing_sqlglot_still_reports_the_older_layers_first_for_unsafe_sql(self):
        with patch.object(sql_ast, "sqlglot", None):
            for unsafe, needle in (
                ("DROP TABLE holdings", "Only read-only SELECT"),
                ("SELECT 1; SELECT 2", "Multiple SQL statements"),
                ("WITH a AS (SELECT 1) DELETE FROM holdings", "DELETE"),
            ):
                with self.assertRaises(ReadOnlyQueryError) as ctx:
                    validate_read_only_sql(unsafe)
                self.assertIn(needle, str(ctx.exception))  # existing error handling is unchanged

    def test_unexpected_validator_failure_also_fails_closed(self):
        with patch.object(sql_ast.sqlglot, "parse", side_effect=RuntimeError("incompatible sqlglot")):
            with self.assertRaises(ReadOnlyQueryError) as ctx:
                validate_read_only_sql("SELECT COUNT(*) FROM portfolios")
        self.assertIn("could not be completed", str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, SqlSyntaxError)

    def test_malformed_and_unsafe_sql_keep_their_error_types_when_sqlglot_is_present(self):
        with self.assertRaises(SqlSyntaxError):
            validate_read_only_sql(Q7_MALFORMED)
        with self.assertRaises(ReadOnlyQueryError) as ctx:
            validate_read_only_sql("SELECT * FROM sqlite_master")
        self.assertNotIsInstance(ctx.exception, SqlSyntaxError)

    def test_without_sqlglot_at_import_time_nothing_can_run(self):
        """Real import failure in a clean interpreter (sys.modules['sqlglot'] = None makes the import raise)."""
        import subprocess

        code = "\n".join([
            "import sys; sys.modules['sqlglot'] = None",
            "from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError",
            "from portfolio_agent import sql_ast",
            "assert not sql_ast.is_available()",
            "db = PortfolioDatabase()",
            "try:",
            "    db.execute_read_only('SELECT COUNT(*) FROM portfolios')",
            "    print('EXECUTED')",
            "except ReadOnlyQueryError as e:",
            "    print('REJECTED:', e)",
        ])
        out = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("REJECTED:", out.stdout)
        self.assertNotIn("EXECUTED", out.stdout)


class TestDatabaseIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_engine_protections_are_untouched(self):
        conn = self.db.get_read_only_connection()
        import sqlite3

        with self.assertRaises(sqlite3.DatabaseError):  # even reading a PRAGMA is denied by the authorizer
            conn.execute("PRAGMA query_only")

        for sql in ("DELETE FROM holdings", "INSERT INTO sectors (sector_id) VALUES (999)", "DROP TABLE holdings"):
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute(sql)  # the authorizer / read-only mode still deny writes on their own

    def test_execute_read_only_rejects_before_touching_the_database(self):
        for sql in ("SELECT * FROM sqlite_master", "SELECT * FROM unknown_table", Q7_MALFORMED, "SELECT * FROM pragma_table_info('holdings')"):
            with self.subTest(sql[:40]), self.assertRaises(ReadOnlyQueryError) as ctx:
                self.db.execute_read_only(sql, ("Technology",) if "?" in sql else None)
            self.assertIn("AST validation", str(ctx.exception))

    def test_bound_parameters_remain_the_only_way_to_pass_values(self):
        attack = "'; DROP TABLE holdings; --"
        res = self.db.execute_read_only("SELECT portfolio_name FROM portfolios WHERE portfolio_name = ?", (attack,))
        self.assertEqual(res["row_count"], 0)
        self.assertEqual(self.db.execute_read_only("SELECT COUNT(*) AS n FROM holdings")["rows"][0]["n"], EXPECTED_TABLE_COUNTS["holdings"])

    def test_results_are_unchanged_by_the_new_layer(self):
        res = self.db.execute_read_only("SELECT COUNT(*) FROM portfolios;")
        self.assertEqual(res["rows"][0]["COUNT(*)"], 13)
        self.assertEqual(res["sql"], "SELECT COUNT(*) FROM portfolios")

    def test_missing_sqlglot_never_reaches_sqlite_execution(self):
        conn = MagicMock()
        with patch.object(PortfolioDatabase, "get_read_only_connection", return_value=conn), patch.object(sql_ast, "sqlglot", None):
            with self.assertRaises(ReadOnlyQueryError) as ctx:
                self.db.execute_read_only("SELECT COUNT(*) FROM portfolios")
        self.assertIn("SQLGlot is required", str(ctx.exception))
        conn.cursor.assert_not_called()  # no statement was ever prepared or executed

    def test_missing_sqlglot_blocks_the_agent_tools_and_gemini_output(self):
        gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=MagicMock(), db=self.db)
        tool = SQLQueryTool(db=self.db, use_gemini=False)
        with patch.object(sql_ast, "sqlglot", None):
            res = tool.execute("How many portfolios do we have in total?")
            self.assertNotEqual(res["status"], "success")
            self.assertEqual(res["rows"], [])
            with self.assertRaises(GeminiGenerationError) as ctx:
                gen.parse_gemini_response(json.dumps({"sql": "SELECT COUNT(*) FROM portfolios", "parameters": [], "description": ""}))
            self.assertEqual(ctx.exception.category, "unsafe_sql")
        # ... and everything works again once SQLGlot is available.
        self.assertEqual(tool.execute("How many portfolios do we have in total?")["status"], "success")

    def test_real_schema_tables_are_discovered(self):
        self.assertEqual(set(self.db._authorised_tables()), set(EXPECTED_TABLE_COUNTS))


class TestOfficialAssessmentQueries(unittest.TestCase):
    """Every query the project itself issues (official ground truth, rule-based, Gemini-shaped) must pass."""

    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.truth = json.loads((PROJECT_ROOT / "ground_truth_dataset.json").read_text(encoding="utf-8"))["questions"]

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def test_all_official_text2sql_queries_pass_and_execute(self):
        for q in self.truth:
            sql = q["ground_truth"].get("sql_query")
            if not sql:
                continue
            with self.subTest(q=q["id"]):
                res = self.db.execute_read_only(sql)
                self.assertGreaterEqual(res["row_count"], 1)

    def test_all_rule_based_queries_pass(self):
        gen = RuleBasedSQLGenerator()
        for q in self.truth:
            if q["type"] != "text2sql":
                continue
            sql, params = gen.generate(q["question"])
            with self.subTest(q=q["id"]):
                validate_read_only_sql(sql)
                self.assertGreaterEqual(self.db.execute_read_only(sql, params)["row_count"], 1)

    def test_gemini_style_q1_to_q8_pass_end_to_end_and_are_reported_as_gemini(self):
        """The official SQL, returned as if by Gemini, goes through the AST layer and counts as Gemini (no fallback)."""
        for q in (x for x in self.truth if x["type"] == "text2sql"):
            official = q["ground_truth"]["sql_query"]
            client = MagicMock()
            client.models.generate_content.return_value = MagicMock(text=json.dumps({"sql": official, "parameters": [], "description": "x"}))
            tool = SQLQueryTool(db=self.db, gemini_generator=GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db), use_gemini=True)
            res = tool.execute(q["question"])
            with self.subTest(q=q["id"]):
                self.assertEqual((res["status"], res["generation_method"], res["fallback_used"]), ("success", "gemini", False))
                self.assertEqual(res["row_count"], self.db.execute_read_only(official)["row_count"])

    def test_repaired_q7_cte_structure_executes_with_gemini_style_parameters(self):
        gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=MagicMock(), db=self.db)
        parsed = gen.parse_gemini_response(json.dumps({"sql": Q7_GEMINI_STYLE, "parameters": ["Technology"], "description": "x"}))
        res = self.db.execute_read_only(parsed["sql"], tuple(parsed["parameters"]))
        self.assertEqual(res["row_count"], 8)


class TestGeminiParserCategories(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = PortfolioDatabase()
        if not cls.db.is_initialized():
            cls.db.initialize_database()
        cls.gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=MagicMock(), db=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def parse(self, sql, params=()):
        return self.gen.parse_gemini_response(json.dumps({"sql": sql, "parameters": list(params), "description": ""}))

    def category(self, sql, params=()):
        with self.assertRaises(GeminiGenerationError) as ctx:
            self.parse(sql, params)
        return ctx.exception.category

    def test_categories(self):
        self.assertEqual(self.category("SELECT * FROM sqlite_master"), "unsafe_sql")
        self.assertEqual(self.category("SELECT * FROM pragma_table_info('holdings')"), "unsafe_sql")
        self.assertEqual(self.category("SELECT * FROM nonexistent"), "unsafe_sql")
        self.assertEqual(self.category("SELECT load_extension('x')"), "unsafe_sql")
        self.assertEqual(self.category("DELETE FROM holdings"), "unsafe_sql")
        self.assertEqual(self.category(Q7_MALFORMED, ["Technology"]), "malformed_sql")
        self.assertEqual(self.category("SELECT * FROM"), "malformed_sql")
        self.assertEqual(self.category("SELECT * FROM portfolios WHERE status = :s", ["Active"]), "invalid_parameters")  # existing, more specific
        self.assertEqual(self.category("SELECT * FROM portfolios WHERE status = ?", []), "invalid_parameters")

    def test_valid_sql_is_returned_unchanged(self):
        out = self.parse(Q7_GEMINI_STYLE, ["Technology"])
        self.assertEqual(out["sql"], Q7_GEMINI_STYLE)
        self.assertEqual(out["parameters"], ["Technology"])

    def test_any_syntax_error_gets_the_single_repair_attempt(self):
        broken = "SELECT portfolio_name FROM portfolios WHERE"
        good = {"sql": "SELECT portfolio_name FROM portfolios", "parameters": [], "description": "x"}
        client = MagicMock()
        client.models.generate_content.side_effect = [MagicMock(text=json.dumps({"sql": broken, "parameters": [], "description": ""})), MagicMock(text=json.dumps(good))]
        gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db)
        self.assertEqual(gen.generate_structured("list portfolios")["sql"], good["sql"])
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_unsafe_sql_is_never_retried(self):
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(text=json.dumps({"sql": "SELECT * FROM sqlite_master", "parameters": [], "description": ""}))
        gen = GeminiSQLGenerator(api_key="unit-test-placeholder", client=client, db=self.db)
        with self.assertRaises(GeminiGenerationError) as ctx:
            gen.generate_structured("show tables")
        self.assertEqual(ctx.exception.category, "unsafe_sql")
        self.assertEqual(client.models.generate_content.call_count, 1)


if __name__ == "__main__":
    unittest.main()

# Project Overview — Technical Summary

Concise technical reference for the PL Capital AI Portfolio Analytics Agent. For setup and usage see
the [README](../README.md); for the original brief see
[ORIGINAL_ASSESSMENT_README.md](ORIGINAL_ASSESSMENT_README.md).

## 1. Database structure

`init_db.py` builds `database/portfolio.db` from the official `database_schema.sql` and the nine CSV
files in `data/`. It builds into a staging file first, enforces foreign keys, checks record counts and
`PRAGMA integrity_check`, and only then replaces the existing database.

| Table | Rows | Key columns / notes |
|---|---:|---|
| `sectors` | 10 | `sector_id`, `sector_name` |
| `securities` | 30 | 25 `Stock` + 5 `Bond`; `asset_type`, `sector_id` (NULL for bonds), `current_price` |
| `benchmarks` | 13 | reference data |
| `portfolios` | 13 | `portfolio_name`, `status`, `target_risk_level`, `total_aum`; `benchmark_index` is free text (no FK) |
| `holdings` | 95 | `portfolio_id`, `security_id`, `quantity`, `current_weight` (fraction: 0.18 = 18%), `cost_basis` |
| `transactions` | 24 | `BUY` / `SELL` |
| `historical_prices` | 330 | daily prices |
| `portfolio_performance` | 44 | four portfolios only |
| `risk_metrics` | 44 | four portfolios only |
| **Total** | **603** | 0 foreign-key violations |

Core relationship: `portfolios → holdings → securities → sectors`.

## 2. Tool responsibilities

| Component | Responsibility | Never does |
|---|---|---|
| `SQLQueryTool` | Gets SQL for a question (Gemini first, rule-based fallback), runs it read-only, returns rows and metadata. | Financial arithmetic in Python. |
| `GeminiSQLGenerator` | Schema-grounded prompt (live schema, categorical value hints, `FINANCIAL_SEMANTICS`, `RESULT_CONVENTIONS`), structured JSON output, validation of SQL/parameters, categorised errors. | Execute SQL; see the API key in logs or errors; contain question IDs, official SQL or expected answers. |
| `RuleBasedSQLGenerator` | Deterministic, parameterised SQL for the eight supplied question patterns; `UnsupportedQuestionError` otherwise. | Guess at unrecognised questions. |
| `ExposureCalculator` | Resolves a portfolio (ID, exact name, unique partial name, token match; rejects ambiguous names) and computes equity sector exposure. | Use Gemini; renormalise weights. |
| `ScenarioTool` / `ScenarioVerifier` | Hypothetical stress tests: sector weight x shock (Decimal, no renormalisation, bonds excluded), independently recomputed from raw rows; failed or unverified results are withheld. | Forecast, use Gemini, or write to the database. |
| `AnswerVerifier` | Checks that the final response is consistent with its structured tool results; withholds inconsistent data; labels model-written text as unverified. | Calculate, query the database, or call Gemini. |
| `HybridAnalysisTool` | For questions that need database data and sector exposure: resolves one portfolio, runs the deterministic exposure (verified) and the SQL tool, and reports `success`, `partial` or `error` without ever presenting a partial result as complete. | Contain calculation or SQL logic, or involve Gemini in exposure. |
| `FinancialVerifier` | Independently recomputes every exposure result (raw rows, Python arithmetic, no shared code with the calculator) and compares it using the evaluator's tolerance; failed results are rejected and unverifiable ones are withheld (both become errors with the figures cleared). | Use Gemini, write to the database, or redefine the exposure rules. |
| `PortfolioDatabase` | Ingestion, schema introspection, SQL validation, read-only execution. | Allow writes on the query path. |

### Response contract

Agent: `{status, question, selected_tool, route_reason, result, error}`.
SQL tool `result`: `status, generated_sql, query_parameters, columns, rows, row_count,
generation_method, model_name, fallback_used, fallback_reason, query_description, error`.
Exposure `result`: `status ('success' | 'no_equity_holdings' | 'error'), portfolio_id,
portfolio_name, exposures[{sector, weight_fraction, exposure_pct, holding_count}],
total_equity_weight_pct, total_holdings_count, equity_holdings_count, excluded_bond_holdings,
unmapped_equity_holdings`.

## 3. Agent routing

`PortfolioAgent` uses keyword rules, not an LLM:

1. If the question contains a **per-portfolio / market-value phrase** (`each portfolio`, `by portfolio`,
   `per portfolio`, `across portfolios`, `total portfolio value`, `market value`), it goes to the **SQL
   tool**. This keeps Q7-style questions away from the exposure calculator.
2. If it contains an **exposure phrase** (`sector exposure`, `sector allocation`, `exposure breakdown`,
   `percentage invested in`, …), it goes to the **Exposure Calculator**, after the portfolio name is
   extracted (quoted name, `portfolio <id>`, `for …`, `in … in …`, `in …`).
3. Everything else goes to the **SQL tool**.

Regression tests cover the boundary cases, e.g. "Show Technology holding value and percentage by
portfolio." → SQL, and "What percentage is invested in Technology in Tech Innovation Fund?" → exposure.

## 4. SQL safety

All SQL, whether rule-based or generated by Gemini, passes through `PortfolioDatabase.execute_read_only`:

1. **Lexical validation** (`validate_read_only_sql`): a single statement starting with `SELECT` or
   `WITH`; no comments; no internal semicolons; no `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/
   ATTACH/DETACH/PRAGMA/VACUUM/...`. String literals are masked before these checks.
2. **AST validation** (`portfolio_agent/sql_ast.py`, SQLGlot with the SQLite dialect; mandatory and fail closed:
   if SQLGlot is missing or the validator errors, the query is rejected and never reaches SQLite): the
   statement must parse as exactly one `SELECT` or set operation (a `WITH` clause is part of it); no
   DML, DDL, `PRAGMA`, `ATTACH`/`DETACH` or transaction node anywhere in the tree, including inside CTEs and
   subqueries; every table must be one of the database's tables or a CTE of the same statement (no
   `sqlite_master`, no schema qualifier other than `main`, no table-valued functions such as
   `pragma_table_info`); no `load_extension`, `readfile`, `writefile`, `zeroblob`/`randomblob`; positional `?`
   placeholders only; the CTE list must be followed by `SELECT`. Unparseable SQL raises `SqlSyntaxError`.
   Column existence is still checked by SQLite itself.
3. **Read-only connection**: SQLite URI `mode=ro`.
4. **`PRAGMA query_only = ON`**.
5. **SQLite authorizer** denying insert/update/delete, DDL, `ATTACH`/`DETACH`, `PRAGMA` and transactions.

Gemini output is validated before it is executed: the JSON shape, the parameter types (scalars only),
positional `?` placeholders only, and a placeholder count equal to the number of parameters. It must
also pass the same read-only check. Literal values are bound as parameters. Unknown tables or columns
fail in the database layer and trigger the fallback. Internal identifiers interpolated into SQL (table
names in integrity checks and CSV ingestion) come from `sqlite_master`, fixed constants, or CSV headers
that are checked against the schema first, never from user input.

## 5. Exposure calculation rules

```
exposure_pct(sector) = SUM(holdings.current_weight) × 100
                       over holdings of the portfolio
                       WHERE securities.asset_type = 'Stock'
                       GROUP BY sectors.sector_name
```

- Bonds are excluded and counted in `excluded_bond_holdings`.
- **No renormalisation**: a 0.36 weight is reported as 36%, even if equities make up less than 100% of
  the portfolio. `total_equity_weight_pct` shows the equity total separately.
- A bond-only portfolio returns `status = 'no_equity_holdings'` with an empty breakdown.
- Stock holdings without a sector are reported in `unmapped_equity_holdings`.

This is deliberately different from **Q7**, which is a SQL question about **market value**:
`SUM(quantity × current_price)` of Technology holdings as a percentage of the total market value of all
holdings, bonds included. **Q8** keeps the supplied diversification definition: distinct sectors among
Stock holdings divided by the count of all holdings, bonds included.

## 6. Evaluator methodology

`evaluate.py` never uses the agent to compute expected answers.

| Question type | Expected answer source | Pass criteria |
|---|---|---|
| `text2sql` (Q1–Q8) | Official `sql_query`, run unchanged on a separate read-only `sqlite3` connection | Correct tool; same row count; values match (abs 1e-6 / rel 1e-9); same order when the official SQL has `ORDER BY`; column names may differ and extra columns are ignored |
| `exposure_calculator` (Q9–Q10) | Recomputed from the original CSVs with the `csv` module (rules in §5) | Correct tool; same portfolio; identical sector set; each % within 1e-4 percentage points; same excluded-bond count |

Each run also checks that the database row counts equal the CSV row counts (603) and that there are no
foreign-key violations. The report records the generation method per question and the Gemini client
state (`none`, `live`, `mock`, `unconfigured`). Mocked or unconfigured runs are never labelled live.
Unit tests confirm that the evaluator detects wrong values, wrong routing, wrong order, missing rows,
extra sectors and agent exceptions.

`--questions IDS` runs a targeted subset (e.g. one live request for Q7). The report is labelled as a
subset run with a "subset score". By default it is written to `evaluation_report_subset.md`, so it
cannot overwrite the full report. Subset scores are never combined with full-run scores.

### Gemini prompt conventions

Besides the live schema and categorical value hints, the system instruction contains general rules
derived from the failures seen in live runs. None of them names a question, portfolio or expected
value:

- **Granularity first:** a total, count or average over a set is one aggregate row.
- **Projection completeness:** holding rows carry `holding_id`, `symbol`, `company_name` and
  `quantity`; percentages come with their numerator and denominator.
- **Deterministic ordering:** grouped aggregates are ordered by the computed metric `DESC`; top-N
  requests are ordered by the ranking metric.
- **Category-exposure scope:** only portfolios with qualifying holdings (inner join, no
  `LEFT JOIN` + `COALESCE` zero rows), unless zero-exposure rows are explicitly requested.
- **Diversification:** distinct Stock sectors ÷ all holdings (bonds included), rounded to 3 dp.

See [PROMPT_IMPROVEMENT_REPORT.md](PROMPT_IMPROVEMENT_REPORT.md).

## 7. Ground-truth mapping

| Q | Ground-truth question | Tool | Key semantics | Deterministic result | Final live Gemini run (2026-11-09) |
|---|---|---|---|---|---|
| 1 | How many portfolios do we have in total? | SQL | `COUNT(*)` | 13 — PASS | PASS |
| 2 | Names of all active portfolios | SQL | `status = 'Active'` | 10 rows — PASS | PASS |
| 3 | Securities in the Technology sector | SQL | join to `sectors` | 8 rows — PASS | PASS |
| 4 | Total AUM, high target risk level | SQL | `SUM(total_aum)`, `'High'` | 85,000,000 — PASS | PASS |
| 5 | Top 5 holdings by cost basis, Growth Equity Fund | SQL | stored `cost_basis`, `ORDER BY … LIMIT 5` | 5 rows — PASS | PASS |
| 6 | Average current price per sector | SQL | `AVG(current_price)` by sector | 7 rows — PASS | PASS |
| 7 | Technology value and % of total portfolio value | SQL | `quantity × current_price`; total includes bonds | 8 rows — PASS | PASS |
| 8 | Portfolios with > 5 sectors, diversification | SQL | distinct Stock sectors ÷ all holdings | 1 row — PASS | PASS |
| 9 | Sector exposures, Tech Innovation Fund | Exposure | equity-only `current_weight` | Technology 100% — PASS | PASS (deterministic) |
| 10 | Sector exposure breakdown, international equity | Exposure | resolves to International Equity Fund | Energy 38%, Technology 36%, Automotive 15%, Consumer Staples 11% — PASS | PASS (deterministic) |
| | **Score** | | | **10/10** | **10/10**, `gemini-3.5-flash-lite`, Q1–Q8 Gemini, 0 fallbacks |

## 8. Known limitations

- **The deterministic 10/10 does not measure Gemini.** The rule-based generator was built around the
  supplied question patterns.
- **Live Gemini status:**
  - The final complete `--use-llm` evaluation (2026-11-09, `gemini-3.5-flash-lite`) scored **10/10**:
    Q1–Q8 answered with Gemini-generated SQL (8/8 pass, 0 fallbacks) and Q9–Q10 by the deterministic
    `ExposureCalculator` (both pass). Report: `docs/EVALUATION_FINAL_LIVE_20261109.md`. It is a single run.
  - An earlier live run scored 9/10 (Q7 failed) and a targeted Q7 re-test then passed; both are historical
    (`evaluation_report.md`, `docs/EVALUATION_Q7_TARGETED.md`) and were superseded by later fixes.
  - **Historical data is very limited:** performance, risk, price and transaction records cover only
    2023-09-25 to 2023-10-09 (performance and risk records for 4 of the 13 portfolios). The agent has no
    comprehensive historical market data.
  - The code default `gemini-3.8-flash` (and `gemini-3.7-flash`) returns server-side 504s for this
    project's key, so `GEMINI_MODEL` is set in `.env`.
- **Gemini output is non-deterministic.** Gemini-mode scores can vary between runs and model versions.
- **Routing is keyword-based.** Unusual phrasings can be misrouted, and hybrid questions that need both
  tools are not supported.
- **Bond exclusion is tested only on synthetic data.** The Q9/Q10 portfolios hold no bonds.
- **No guarded ties under `ORDER BY`.** Rows tied on the sort key could legitimately appear in a
  different order; no supplied question has such a tie.
- **Not a production system.** Single-user CLI over a sample dataset: no authentication, concurrency
  handling or REST API, no trading, and no investment advice.

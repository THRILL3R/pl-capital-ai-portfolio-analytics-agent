# PL Capital — Full Q1–Q10 Assessment with gemini-4-argon

Run: 2026-11-08 22:11 UTC · Q1–Q8: `gemini-4-argon` via the 1min.AI gateway (`https://api.1min.ai/api/chat-with-ai`) · sent to Argon in this run: Q4, Q5, Q6, Q7, Q8 (1 question request(s), plus 1 preflight probe) · carried over from the previous recorded run (SQL re-executed locally, no request): Q1, Q2, Q3 · Q9–Q10: deterministic `ExposureCalculator` (no LLM). Generated entirely by `scripts/test_argon_full_assessment.py`. No API key appears in this report.

Preflight probe: OK (4.05s) — HTTP 200, text 'OK', record status 'SUCCESS'

> **Run stopped early** (gateway / credit / quota / auth): Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILURE"}; top-level keys: ['aiRecord'])

## Overall score

| Scope | Score |
|---|---:|
| **Q1–Q10 overall** | **5/10** |
| Q1–Q8 text-to-SQL (Argon, production prompt) | 3/8 |
| Q1–Q8 not evaluated | 5 |
| Q9–Q10 exposure (deterministic) | 2/2 |

Argon latency (answered requests only): min 4.09s · median 7.27s · max 7.28s

## Scorecard

| Q | Source | Tool (expected → routed) | Generation | Parse | Safety | Execution | Rows exp/act | Latency | Ground truth |
|---|---|---|---|---|---|---|---|---:|---|
| Q1 | carried over | sql_query → sql_query | success (recorded in previous run; not re-sent) | accepted (previous run: accepted) | ACCEPTED | success | 1/1 | 7.27s | **PASS** |
| Q2 | carried over | sql_query → sql_query | success (recorded in previous run; not re-sent) | accepted (previous run: accepted) | ACCEPTED | success | 10/10 | 4.09s | **PASS** |
| Q3 | carried over | sql_query → sql_query | success (recorded in previous run; not re-sent) | accepted (previous run: accepted) | ACCEPTED | success | 8/8 | 7.28s | **PASS** |
| Q4 | this run | sql_query → sql_query | failed (gateway returned no content) | not run | not run | not run | 1/— | 0.37s | **NOT EVALUATED** |
| Q5 | not attempted | sql_query → sql_query | not run | not run | not run | not run | 5/— | — | **NOT EVALUATED** |
| Q6 | not attempted | sql_query → sql_query | not run | not run | not run | not run | 7/— | — | **NOT EVALUATED** |
| Q7 | not attempted | sql_query → sql_query | not run | not run | not run | not run | 8/— | — | **NOT EVALUATED** |
| Q8 | not attempted | sql_query → sql_query | not run | not run | not run | not run | 1/— | — | **NOT EVALUATED** |
| Q9 | deterministic | exposure_calculator → exposure_calculator | n/a | n/a | n/a | success | 1/1 sectors | 0.00s | **PASS** |
| Q10 | deterministic | exposure_calculator → exposure_calculator | n/a | n/a | n/a | success | 4/4 sectors | 0.00s | **PASS** |

## Method

- **Prompt (Q1–Q8):** the production `GeminiSQLGenerator.build_system_instruction()` — live schema introspected from `database/portfolio.db`, the production `FINANCIAL_SEMANTICS` rules, categorical value hints and the strict output rules — plus the production `RESPONSE_JSON_SCHEMA` and user prompt `Question: …`. No expected answers or official SQL are in the prompt.
- **Gateway difference:** the Google API takes `system_instruction` and a JSON response schema natively; 1min.AI takes one prompt string, so both are sent as text. JSON output is therefore requested, not enforced by the gateway; the production parser still enforces it.
- **Validation:** production `parse_gemini_response()` (JSON shape, parameter types, positional placeholders, placeholder count, `validate_read_only_sql()`), then an explicit `validate_read_only_sql()` re-check, then `PortfolioDatabase.execute_read_only(sql, params)` (`mode=ro`, `query_only`, authorizer).
- **Scoring:** `evaluate.compare_tables()` against the official SQL run by `evaluate.run_expected_sql()` — abs 1e-06 / rel 1e-09; all expected columns required (names may differ, extras ignored); ordered when the official SQL has `ORDER BY`. Routing is checked with the production agent's router.
- **Q9–Q10:** `evaluate.evaluate_question()` with `PortfolioAgent` (Gemini disabled) → `ExposureCalculator`; expected values computed independently from the CSVs (tolerance 0.0001 pp). The ground-truth file defines the target portfolio and rules, not numeric values.
- One request per question, sequential, no retries; an empty gateway reply or a credit / quota / auth error stops the run and later questions are marked NOT EVALUATED.
- Carried-over questions reuse the SQL recorded by the previous run's CSV; they are re-validated and re-executed locally against the current database and re-scored with the same evaluator rules.

## Comparison with the previous Argon run

Previous run: schema-only prompt, plain-SQL output (Q1 via `test_argon_portfolio.py`, Q2–Q8 via `test_argon_q2_q8.py`). This run: production prompt with financial semantics, value hints and JSON output.

| Q | Previous | Previous issue | This run | Latency prev → now |
|---|---|---|---|---|
| Q1 | PASS | — | **PASS** | 4.71s → 7.27s |
| Q2 | PASS | — | **PASS** | 3.20s → 4.09s |
| Q3 | PASS | — | **PASS** | 4.54s → 7.28s |
| Q4 | PASS | — | **NOT EVALUATED** | 5.89s → 0.37s |
| Q5 | FAIL | omitted holding_id | **NOT EVALUATED** | 3.67s → — |
| Q6 | FAIL | no ORDER BY | **NOT EVALUATED** | 6.32s → — |
| Q7 | FAIL | 13 rows incl. 0% portfolios, no ORDER BY | **NOT EVALUATED** | 4.87s → — |
| Q8 | FAIL | own metrics; no official diversification ratio | **NOT EVALUATED** | 4.62s → — |
| **Total** | **4/8** | | **3/8** (5 not evaluated) | |

## Q1–Q8 details

### Q1 — How many portfolios do we have in total?

**PASS** · generation success (recorded in previous run; not re-sent) · parse accepted (previous run: accepted) · safety ACCEPTED · execution success · latency 7.27s

Argon SQL:

```sql
SELECT COUNT(*) AS total_portfolios FROM portfolios;
```

Model description: _Returns the total count of portfolios in the database._

<details><summary>Official SQL</summary>

```sql
SELECT COUNT(*) FROM portfolios;
```
</details>

Expected (official SQL):

| COUNT(*) |
|---|
| 13 |

Actual (Argon SQL):

| total_portfolios |
|---|
| 13 |

- ℹ️ Carried over: recorded SQL re-validated and re-executed locally; no new API request.
- ℹ️ Column names differ from the official query (cosmetic; values match).

### Q2 — What are the names of all active portfolios?

**PASS** · generation success (recorded in previous run; not re-sent) · parse accepted (previous run: accepted) · safety ACCEPTED · execution success · latency 4.09s

Argon SQL · parameters `["Active"]`:

```sql
SELECT portfolio_name FROM portfolios WHERE status = ? ORDER BY portfolio_name
```

Model description: _Returns the names of all portfolios with an 'Active' status._

<details><summary>Official SQL</summary>

```sql
SELECT portfolio_name FROM portfolios WHERE status = 'Active';
```
</details>

Expected (official SQL):

| portfolio_name |
|---|
| Growth Equity Fund |
| Conservative Income Fund |
| Tech Innovation Fund |
| Balanced Portfolio |
| ESG Sustainable Fund |
| Small Cap Value Fund |
| International Equity Fund |
| Fixed Income Plus |
| Dividend Aristocrats Fund |
| Emerging Markets Fund |

Actual (Argon SQL):

| portfolio_name |
|---|
| Balanced Portfolio |
| Conservative Income Fund |
| Dividend Aristocrats Fund |
| ESG Sustainable Fund |
| Emerging Markets Fund |
| Fixed Income Plus |
| Growth Equity Fund |
| International Equity Fund |
| Small Cap Value Fund |
| Tech Innovation Fund |

- ℹ️ Carried over: recorded SQL re-validated and re-executed locally; no new API request.

### Q3 — Which securities are in the Technology sector?

**PASS** · generation success (recorded in previous run; not re-sent) · parse accepted (previous run: accepted) · safety ACCEPTED · execution success · latency 7.28s

Argon SQL · parameters `["Technology"]`:

```sql
SELECT s.security_id, s.symbol, s.company_name, s.asset_type, s.current_price, sec.sector_name FROM securities s JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = ? ORDER BY s.symbol
```

Model description: _Returns all securities belonging to the Technology sector along with their symbol, company name, asset type, and current price._

<details><summary>Official SQL</summary>

```sql
SELECT s.symbol, s.company_name FROM securities s JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = 'Technology';
```
</details>

Expected (official SQL):

| symbol | company_name |
|---|---|
| AAPL | Apple Inc. |
| MSFT | Microsoft Corporation |
| GOOGL | Alphabet Inc. |
| META | Meta Platforms Inc. |
| NVDA | NVIDIA Corporation |
| AVGO | Broadcom Inc. |
| CRM | Salesforce Inc. |
| ORCL | Oracle Corporation |

Actual (Argon SQL):

| security_id | symbol | company_name | asset_type | current_price | sector_name |
|---|---|---|---|---|---|
| 1 | AAPL | Apple Inc. | Stock | 185.5 | Technology |
| 18 | AVGO | Broadcom Inc. | Stock | 875.15 | Technology |
| 19 | CRM | Salesforce Inc. | Stock | 210.9 | Technology |
| 3 | GOOGL | Alphabet Inc. | Stock | 2,750.8 | Technology |
| 6 | META | Meta Platforms Inc. | Stock | 325.2 | Technology |
| 2 | MSFT | Microsoft Corporation | Stock | 415.26 | Technology |
| 7 | NVDA | NVIDIA Corporation | Stock | 875.3 | Technology |
| 24 | ORCL | Oracle Corporation | Stock | 115.85 | Technology |

- ℹ️ Carried over: recorded SQL re-validated and re-executed locally; no new API request.
- ℹ️ Columns matched by value: symbol -> symbol, company_name -> company_name (4 extra column(s) ignored).
- ℹ️ Column names differ from the official query (cosmetic; values match).

### Q4 — What is the total Assets Under Management (AUM) for portfolios with high target risk level?

**NOT EVALUATED** · generation failed (gateway returned no content) · parse not run · safety not run · execution not run · latency 0.37s

<details><summary>Official SQL</summary>

```sql
SELECT SUM(total_aum) FROM portfolios WHERE target_risk_level = 'High';
```
</details>

Expected (official SQL):

| SUM(total_aum) |
|---|
| 85,000,000 |

- ❌ Gateway error, model output not available: Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILURE"}; top-level keys: ['aiRecord'])

### Q5 — Show me the top 5 holdings by cost basis in the Growth Equity Fund

**NOT EVALUATED** · generation not run · parse not run · safety not run · execution not run · latency —

<details><summary>Official SQL</summary>

```sql
SELECT h.holding_id, s.symbol, s.company_name, h.quantity, h.cost_basis FROM holdings h JOIN securities s ON h.security_id = s.security_id JOIN portfolios p ON h.portfolio_id = p.portfolio_id WHERE p.portfolio_name = 'Growth Equity Fund' ORDER BY h.cost_basis DESC LIMIT 5;
```
</details>

Expected (official SQL):

| holding_id | symbol | company_name | quantity | cost_basis |
|---|---|---|---|---|
| 1 | AAPL | Apple Inc. | 2,500 | 438,000 |
| 8 | AMZN | Amazon.com Inc. | 100 | 320,000 |
| 2 | MSFT | Microsoft Corporation | 800 | 304,400 |
| 3 | NVDA | NVIDIA Corporation | 400 | 288,040 |
| 6 | CRM | Salesforce Inc. | 1,000 | 185,400 |

- ❌ Not run: evaluation stopped after a gateway/credit/quota/auth error (Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILUR).

### Q6 — What is the average current price of securities in each sector?

**NOT EVALUATED** · generation not run · parse not run · safety not run · execution not run · latency —

<details><summary>Official SQL</summary>

```sql
SELECT sec.sector_name, AVG(s.current_price) as avg_price FROM securities s JOIN sectors sec ON s.sector_id = sec.sector_id GROUP BY sec.sector_name ORDER BY avg_price DESC;
```
</details>

Expected (official SQL):

| sector_name | avg_price |
|---|---|
| Consumer Discretionary | 3,380 |
| Technology | 719.245 |
| Healthcare | 285.17 |
| Automotive | 248.5 |
| Financials | 201.2125 |
| Consumer Staples | 173.8625 |
| Energy | 128.575 |

- ❌ Not run: evaluation stopped after a gateway/credit/quota/auth error (Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILUR).

### Q7 — For each portfolio, show the total value of Technology sector holdings and what percentage it represents of the total portfolio value

**NOT EVALUATED** · generation not run · parse not run · safety not run · execution not run · latency —

<details><summary>Official SQL</summary>

```sql
WITH portfolio_tech_value AS (SELECT p.portfolio_id, p.portfolio_name, SUM(h.quantity * s.current_price) as tech_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = 'Technology' GROUP BY p.portfolio_id, p.portfolio_name), portfolio_total_value AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) as total_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id) SELECT ptv.portfolio_name, ptv.tech_value, ptv2.total_value, ROUND((ptv.tech_value / ptv2.total_value) * 100, 2) as tech_percentage FROM portfolio_tech_value ptv JOIN portfolio_total_value ptv2 ON ptv.portfolio_id = ptv2.portfolio_id ORDER BY tech_percentage DESC;
```
</details>

Expected (official SQL):

| portfolio_name | tech_value | total_value | tech_percentage |
|---|---|---|---|
| Tech Innovation Fund | 1,565,292.5 | 1,565,292.5 | 100 |
| Growth Equity Fund | 1,866,148 | 2,204,148 | 84.67 |
| Total International Index Fund | 1,427,086 | 2,281,686 | 62.55 |
| International Equity Fund | 346,888 | 734,928 | 47.2 |
| Total Stock Market Index Fund | 3,093,520 | 6,622,340 | 46.71 |
| Balanced Portfolio | 388,704 | 1,073,279 | 36.22 |
| Emerging Markets Fund | 339,095 | 1,045,100 | 32.45 |
| ESG Sustainable Fund | 272,978 | 1,007,228 | 27.1 |

- ❌ Not run: evaluation stopped after a gateway/credit/quota/auth error (Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILUR).

### Q8 — Find portfolios that have holdings in more than 5 different sectors and show their diversification metrics

**NOT EVALUATED** · generation not run · parse not run · safety not run · execution not run · latency —

<details><summary>Official SQL</summary>

```sql
WITH portfolio_sector_count AS (SELECT p.portfolio_id, p.portfolio_name, COUNT(DISTINCT sec.sector_id) as sector_count FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE s.asset_type = 'Stock' GROUP BY p.portfolio_id, p.portfolio_name HAVING COUNT(DISTINCT sec.sector_id) > 5), portfolio_holdings_count AS (SELECT p.portfolio_id, COUNT(h.holding_id) as total_holdings FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id GROUP BY p.portfolio_id) SELECT psc.portfolio_name, psc.sector_count, phc.total_holdings, ROUND(CAST(psc.sector_count AS REAL) / phc.total_holdings, 3) as diversification_ratio FROM portfolio_sector_count psc JOIN portfolio_holdings_count phc ON psc.portfolio_id = phc.portfolio_id ORDER BY diversification_ratio DESC;
```
</details>

Expected (official SQL):

| portfolio_name | sector_count | total_holdings | diversification_ratio |
|---|---|---|---|
| Total Stock Market Index Fund | 6 | 12 | 0.5 |

- ❌ Not run: evaluation stopped after a gateway/credit/quota/auth error (Gemini API error (api_error): OneMinAPIError: 1min.AI returned HTTP 200 with no content (diagnostics: {"status": "FAILUR).

## Q9–Q10 details (deterministic ExposureCalculator)

### Q9 — What are the sector exposures for the Tech Innovation Fund?

**PASS** · resolved portfolio **Tech Innovation Fund** · routed to `exposure_calculator` · no Argon request

Expected (computed from CSVs):

| sector | exposure_pct |
|---|---|
| Technology | 100 |

Actual (ExposureCalculator):

| sector | exposure_pct |
|---|---|
| Technology | 100 |

| Check | Expected | Actual |
|---|---:|---:|
| equity_holdings_count | 8 | 8 |
| excluded_bond_holdings | 0 | 0 |
| total_equity_weight_pct | 100 | 100 |
| total_holdings_count | — | 8 |

- ℹ️ Expected computed from original CSVs: 1 sector(s), 8 equity holding(s), 0 bond holding(s) excluded, total equity weight 100.00% (not renormalised).

### Q10 — Calculate the sector exposure breakdown for international equity

**PASS** · resolved portfolio **International Equity Fund** · routed to `exposure_calculator` · no Argon request

Expected (computed from CSVs):

| sector | exposure_pct |
|---|---|
| Energy | 38 |
| Technology | 36 |
| Automotive | 15 |
| Consumer Staples | 11 |

Actual (ExposureCalculator):

| sector | exposure_pct |
|---|---|
| Energy | 38 |
| Technology | 36 |
| Automotive | 15 |
| Consumer Staples | 11 |

| Check | Expected | Actual |
|---|---:|---:|
| equity_holdings_count | 8 | 8 |
| excluded_bond_holdings | 0 | 0 |
| total_equity_weight_pct | 100 | 100 |
| total_holdings_count | — | 8 |

- ℹ️ Expected computed from original CSVs: 4 sector(s), 8 equity holding(s), 0 bond holding(s) excluded, total equity weight 100.00% (not renormalised).

### Supplementary real-data bond-exclusion check (not an official question)

Q9 and Q10 portfolios hold no bonds, so bond exclusion is not exercised by them. The same calculator on **Balanced Portfolio**: 4 equity + 4 bond holdings (CSV) → calculator reported 4 equity, 4 bonds excluded, total equity weight 36% (not renormalised to 100%). Sector values match the CSV computation: **yes**.

| sector | exposure_pct (calculator) | expected (CSV) |
|---|---|---|
| Technology | 22 | 22 |
| Financials | 8 | 8 |
| Healthcare | 6 | 6 |

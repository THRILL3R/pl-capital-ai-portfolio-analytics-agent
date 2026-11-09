# Evaluation Report - PL Capital AI Portfolio Analytics Agent

**Overall score: 10/10** (100%)

| Item | Value |
|---|---|
| Evaluation mode | Gemini-enabled SQL generation (live API) |
| Gemini model | `gemini-3.5-flash-lite` |
| Run started | 2026-11-09T18:45:52+05:30 |
| Duration | 12.01 s |
| Python | 3.13.16 (Windows) |
| Ground truth | `ground_truth_dataset.json` (10 questions) |
| Database | `database\portfolio.db` |
| Data integrity | 603 DB rows vs 603 CSV rows across 9 tables (match); 0 foreign-key violations |

## Method

- **Q1-Q8 (text2sql):** the official `sql_query` from the ground truth is executed unchanged on an independent read-only `sqlite3` connection. The agent's answer must have the same row count and matching values (numeric tolerance: abs 1e-6, rel 1e-9). When the official query contains `ORDER BY`, row order must also match. Column aliases are not required to match.
- **Q9-Q10 (exposure_calculator):** expected exposures are recomputed from the original CSV files (`holdings`, `securities`, `sectors`, `portfolios`): `SUM(current_weight) x 100` per sector for `asset_type = 'Stock'` only, bonds excluded, **no renormalisation**. The agent must resolve the ground-truth portfolio, return exactly these sectors (tolerance 0.0001 percentage points) and report the same number of excluded bond holdings.
- **Routing:** every question must be routed to the tool named by its ground-truth `type`.
- The agent is never used to compute expected results.

## Results Summary

| Q | Type | Difficulty | Expected tool | Selected tool | Generation | Rows (exp/act) | Result |
|---|---|---|---|---|---|---|---|
| Q1 | text2sql | easy | sql_query | sql_query | gemini | 1/1 | PASS |
| Q2 | text2sql | easy | sql_query | sql_query | gemini | 10/10 | PASS |
| Q3 | text2sql | easy | sql_query | sql_query | gemini | 8/8 | PASS |
| Q4 | text2sql | medium | sql_query | sql_query | gemini | 1/1 | PASS |
| Q5 | text2sql | medium | sql_query | sql_query | gemini | 5/5 | PASS |
| Q6 | text2sql | medium | sql_query | sql_query | gemini | 7/7 | PASS |
| Q7 | text2sql | hard | sql_query | sql_query | gemini | 8/8 | PASS |
| Q8 | text2sql | hard | sql_query | sql_query | gemini | 1/1 | PASS |
| Q9 | exposure_calculator | medium | exposure_calculator | exposure_calculator | deterministic | 1/1 | PASS |
| Q10 | exposure_calculator | medium | exposure_calculator | exposure_calculator | deterministic | 4/4 | PASS |

## Failed Questions

None. All questions passed.

## Question Details

### Q1 - PASS

**Question:** How many portfolios do we have in total?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 2283.6 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT COUNT(*) AS total_portfolios FROM portfolios
```

Expected result:

| COUNT(*) |
|---|
| 13 |

Actual result:

| total_portfolios |
|---|
| 13 |

Notes:

- Column names differ from the official query (cosmetic; values match).

### Q2 - PASS

**Question:** What are the names of all active portfolios?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1435.9 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT portfolio_name FROM portfolios WHERE status = ?
```

Expected result:

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

Actual result:

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

### Q3 - PASS

**Question:** Which securities are in the Technology sector?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1142.7 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT s.symbol, s.company_name FROM securities s JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = ?
```

Expected result:

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

Actual result:

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

### Q4 - PASS

**Question:** What is the total Assets Under Management (AUM) for portfolios with high target risk level?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1157.1 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT SUM(total_aum) AS total_aum_sum FROM portfolios WHERE target_risk_level = ?
```

Expected result:

| SUM(total_aum) |
|---|
| 85,000,000 |

Actual result:

| total_aum_sum |
|---|
| 85,000,000 |

Notes:

- Column names differ from the official query (cosmetic; values match).

### Q5 - PASS

**Question:** Show me the top 5 holdings by cost basis in the Growth Equity Fund

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1588.8 ms
- Ordered comparison: yes

Agent SQL:

```sql
SELECT h.holding_id, s.symbol, s.company_name, h.quantity, h.cost_basis FROM holdings h JOIN portfolios p ON h.portfolio_id = p.portfolio_id JOIN securities s ON h.security_id = s.security_id WHERE p.portfolio_name = ? ORDER BY h.cost_basis DESC LIMIT ?
```

Expected result:

| holding_id | symbol | company_name | quantity | cost_basis |
|---|---|---|---|---|
| 1 | AAPL | Apple Inc. | 2,500 | 438,000 |
| 8 | AMZN | Amazon.com Inc. | 100 | 320,000 |
| 2 | MSFT | Microsoft Corporation | 800 | 304,400 |
| 3 | NVDA | NVIDIA Corporation | 400 | 288,040 |
| 6 | CRM | Salesforce Inc. | 1,000 | 185,400 |

Actual result:

| holding_id | symbol | company_name | quantity | cost_basis |
|---|---|---|---|---|
| 1 | AAPL | Apple Inc. | 2,500 | 438,000 |
| 8 | AMZN | Amazon.com Inc. | 100 | 320,000 |
| 2 | MSFT | Microsoft Corporation | 800 | 304,400 |
| 3 | NVDA | NVIDIA Corporation | 400 | 288,040 |
| 6 | CRM | Salesforce Inc. | 1,000 | 185,400 |

Notes:

- Row order verified (official query uses ORDER BY).

### Q6 - PASS

**Question:** What is the average current price of securities in each sector?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1303.2 ms
- Ordered comparison: yes

Agent SQL:

```sql
SELECT t2.sector_name, AVG(t1.current_price) AS average_current_price FROM securities AS t1 JOIN sectors AS t2 ON t1.sector_id = t2.sector_id GROUP BY t2.sector_id, t2.sector_name ORDER BY average_current_price DESC
```

Expected result:

| sector_name | avg_price |
|---|---|
| Consumer Discretionary | 3,380 |
| Technology | 719.245 |
| Healthcare | 285.17 |
| Automotive | 248.5 |
| Financials | 201.2125 |
| Consumer Staples | 173.8625 |
| Energy | 128.575 |

Actual result:

| sector_name | average_current_price |
|---|---|
| Consumer Discretionary | 3,380 |
| Technology | 719.245 |
| Healthcare | 285.17 |
| Automotive | 248.5 |
| Financials | 201.2125 |
| Consumer Staples | 173.8625 |
| Energy | 128.575 |

Notes:

- Row order verified (official query uses ORDER BY).
- Column names differ from the official query (cosmetic; values match).

### Q7 - PASS

**Question:** For each portfolio, show the total value of Technology sector holdings and what percentage it represents of the total portfolio value

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1433.9 ms
- Ordered comparison: yes

Agent SQL:

```sql
WITH portfolio_tech AS (SELECT p.portfolio_id, p.portfolio_name, SUM(h.quantity * s.current_price) AS tech_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = ? GROUP BY p.portfolio_id, p.portfolio_name), portfolio_total AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) AS total_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id) SELECT t.portfolio_name, t.tech_value AS numerator_tech_value, tot.total_value AS denominator_total_value, ROUND((t.tech_value * 100.0) / tot.total_value, 2) AS percentage FROM portfolio_tech t JOIN portfolio_total tot ON t.portfolio_id = tot.portfolio_id ORDER BY percentage DESC
```

Expected result:

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

Actual result:

| portfolio_name | numerator_tech_value | denominator_total_value | percentage |
|---|---|---|---|
| Tech Innovation Fund | 1,565,292.5 | 1,565,292.5 | 100 |
| Growth Equity Fund | 1,866,148 | 2,204,148 | 84.67 |
| Total International Index Fund | 1,427,086 | 2,281,686 | 62.55 |
| International Equity Fund | 346,888 | 734,928 | 47.2 |
| Total Stock Market Index Fund | 3,093,520 | 6,622,340 | 46.71 |
| Balanced Portfolio | 388,704 | 1,073,279 | 36.22 |
| Emerging Markets Fund | 339,095 | 1,045,100 | 32.45 |
| ESG Sustainable Fund | 272,978 | 1,007,228 | 27.1 |

Notes:

- Row order verified (official query uses ORDER BY).
- Column names differ from the official query (cosmetic; values match).

### Q8 - PASS

**Question:** Find portfolios that have holdings in more than 5 different sectors and show their diversification metrics

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 1649.7 ms
- Ordered comparison: yes

Agent SQL:

```sql
WITH portfolio_sectors AS (SELECT p.portfolio_id, p.portfolio_name, COUNT(DISTINCT s.sector_id) AS sector_count FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities sec ON h.security_id = sec.security_id JOIN sectors s ON sec.sector_id = s.sector_id WHERE sec.asset_type = ? GROUP BY p.portfolio_id, p.portfolio_name), portfolio_totals AS (SELECT p.portfolio_id, COUNT(h.holding_id) AS total_holdings FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id GROUP BY p.portfolio_id) SELECT ps.portfolio_name, ps.sector_count, pt.total_holdings, ROUND(CAST(ps.sector_count AS REAL) / pt.total_holdings, 3) AS diversification_ratio FROM portfolio_sectors ps JOIN portfolio_totals pt ON ps.portfolio_id = pt.portfolio_id WHERE ps.sector_count > ? ORDER BY diversification_ratio DESC
```

Expected result:

| portfolio_name | sector_count | total_holdings | diversification_ratio |
|---|---|---|---|
| Total Stock Market Index Fund | 6 | 12 | 0.5 |

Actual result:

| portfolio_name | sector_count | total_holdings | diversification_ratio |
|---|---|---|---|
| Total Stock Market Index Fund | 6 | 12 | 0.5 |

Notes:

- Row order verified (official query uses ORDER BY).

### Q9 - PASS

**Question:** What are the sector exposures for the Tech Innovation Fund?

- Selected tool: `exposure_calculator`
- Agent status: `success`
- Generation method: `deterministic`
- Agent time: 3.7 ms

Expected result:

| sector | exposure_pct |
|---|---|
| Technology | 100 |

Actual result:

| sector | exposure_pct |
|---|---|
| Technology | 100 |

Notes:

- Expected computed from original CSVs: 1 sector(s), 8 equity holding(s), 0 bond holding(s) excluded, total equity weight 100.00% (not renormalised).

### Q10 - PASS

**Question:** Calculate the sector exposure breakdown for international equity

- Selected tool: `exposure_calculator`
- Agent status: `success`
- Generation method: `deterministic`
- Agent time: 1.9 ms

Expected result:

| sector | exposure_pct |
|---|---|
| Energy | 38 |
| Technology | 36 |
| Automotive | 15 |
| Consumer Staples | 11 |

Actual result:

| sector | exposure_pct |
|---|---|
| Energy | 38 |
| Technology | 36 |
| Automotive | 15 |
| Consumer Staples | 11 |

Notes:

- Expected computed from original CSVs: 4 sector(s), 8 equity holding(s), 0 bond holding(s) excluded, total equity weight 100.00% (not renormalised).

## Verification Status

- Gemini was enabled and live API requests were attempted with model `gemini-3.5-flash-lite`. 8 of 8 SQL questions were answered with Gemini-generated SQL; 0 used the rule-based fallback (reasons listed per question).
- Exposure questions (Q9-Q10) are always computed deterministically by the ExposureCalculator; Gemini is never used for financial calculations.

## Limitations

- The ground truth defines 10 questions. A perfect score shows the agent reproduces these answers; it does not prove correctness on unseen questions.
- In deterministic mode the rule-based generator emits SQL equivalent to the official queries for the supported question patterns; questions outside those patterns return `unsupported`.
- Gemini-generated SQL is non-deterministic. Results from `--use-llm` runs can vary between runs and model versions, and depend on API availability (timeouts fall back to rule-based SQL, which is reported per question).
- Comparison ignores column names and tolerates extra columns, but requires all expected values, the exact row count and, where the official query specifies `ORDER BY`, the same row order. Rows tied on the ORDER BY key could legitimately appear in a different order; none of the supplied questions currently rely on this.
- Q7 is evaluated with the official market-value definition (quantity x current_price, as a share of total portfolio value including bonds) and Q8 with the supplied diversification semantics (distinct Stock sectors / all holdings). These are intentionally different from the weight-based exposure calculation used for Q9-Q10.

# Evaluation Report - PL Capital AI Portfolio Analytics Agent

**Overall score: 10/10** (100%)

| Item | Value |
|---|---|
| Evaluation mode | Deterministic (rule-based SQL generation, no API key required) |
| Run started | 2026-11-09T04:08:58+05:30 |
| Duration | 0.01 s |
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
| Q1 | text2sql | easy | sql_query | sql_query | rule_based | 1/1 | PASS |
| Q2 | text2sql | easy | sql_query | sql_query | rule_based | 10/10 | PASS |
| Q3 | text2sql | easy | sql_query | sql_query | rule_based | 8/8 | PASS |
| Q4 | text2sql | medium | sql_query | sql_query | rule_based | 1/1 | PASS |
| Q5 | text2sql | medium | sql_query | sql_query | rule_based | 5/5 | PASS |
| Q6 | text2sql | medium | sql_query | sql_query | rule_based | 7/7 | PASS |
| Q7 | text2sql | hard | sql_query | sql_query | rule_based | 8/8 | PASS |
| Q8 | text2sql | hard | sql_query | sql_query | rule_based | 1/1 | PASS |
| Q9 | exposure_calculator | medium | exposure_calculator | exposure_calculator | deterministic | 1/1 | PASS |
| Q10 | exposure_calculator | medium | exposure_calculator | exposure_calculator | deterministic | 4/4 | PASS |

## Failed Questions

None. All questions passed.

## Question Details

### Q1 - PASS

**Question:** How many portfolios do we have in total?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `rule_based`
- Agent time: 0.6 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT COUNT(*) FROM portfolios
```

Expected result:

| COUNT(*) |
|---|
| 13 |

Actual result:

| COUNT(*) |
|---|
| 13 |

### Q2 - PASS

**Question:** What are the names of all active portfolios?

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `rule_based`
- Agent time: 0.1 ms
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
- Generation method: `rule_based`
- Agent time: 0.1 ms
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
- Generation method: `rule_based`
- Agent time: 0.1 ms
- Ordered comparison: no

Agent SQL:

```sql
SELECT SUM(total_aum) FROM portfolios WHERE target_risk_level = ?
```

Expected result:

| SUM(total_aum) |
|---|
| 85,000,000 |

Actual result:

| SUM(total_aum) |
|---|
| 85,000,000 |

### Q5 - PASS

**Question:** Show me the top 5 holdings by cost basis in the Growth Equity Fund

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `rule_based`
- Agent time: 0.2 ms
- Ordered comparison: yes

Agent SQL:

```sql
SELECT h.holding_id, s.symbol, s.company_name, h.quantity, h.cost_basis FROM holdings h JOIN securities s ON h.security_id = s.security_id JOIN portfolios p ON h.portfolio_id = p.portfolio_id WHERE p.portfolio_name = ? ORDER BY h.cost_basis DESC LIMIT ?
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
- Generation method: `rule_based`
- Agent time: 0.1 ms
- Ordered comparison: yes

Agent SQL:

```sql
SELECT sec.sector_name, AVG(s.current_price) as avg_price FROM securities s JOIN sectors sec ON s.sector_id = sec.sector_id GROUP BY sec.sector_name ORDER BY avg_price DESC
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

| sector_name | avg_price |
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

### Q7 - PASS

**Question:** For each portfolio, show the total value of Technology sector holdings and what percentage it represents of the total portfolio value

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `rule_based`
- Agent time: 0.3 ms
- Ordered comparison: yes

Agent SQL:

```sql
WITH portfolio_tech_value AS (SELECT p.portfolio_id, p.portfolio_name, SUM(h.quantity * s.current_price) as tech_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = ? GROUP BY p.portfolio_id, p.portfolio_name), portfolio_total_value AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) as total_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id) SELECT ptv.portfolio_name, ptv.tech_value, ptv2.total_value, ROUND((ptv.tech_value / ptv2.total_value) * 100, 2) as tech_percentage FROM portfolio_tech_value ptv JOIN portfolio_total_value ptv2 ON ptv.portfolio_id = ptv2.portfolio_id ORDER BY tech_percentage DESC
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

Notes:

- Row order verified (official query uses ORDER BY).

### Q8 - PASS

**Question:** Find portfolios that have holdings in more than 5 different sectors and show their diversification metrics

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `rule_based`
- Agent time: 0.2 ms
- Ordered comparison: yes

Agent SQL:

```sql
WITH portfolio_sector_count AS (SELECT p.portfolio_id, p.portfolio_name, COUNT(DISTINCT sec.sector_id) as sector_count FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE s.asset_type = ? GROUP BY p.portfolio_id, p.portfolio_name HAVING COUNT(DISTINCT sec.sector_id) > ?), portfolio_holdings_count AS (SELECT p.portfolio_id, COUNT(h.holding_id) as total_holdings FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id GROUP BY p.portfolio_id) SELECT psc.portfolio_name, psc.sector_count, phc.total_holdings, ROUND(CAST(psc.sector_count AS REAL) / phc.total_holdings, 3) as diversification_ratio FROM portfolio_sector_count psc JOIN portfolio_holdings_count phc ON psc.portfolio_id = phc.portfolio_id ORDER BY diversification_ratio DESC
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
- Agent time: 0.6 ms

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
- Agent time: 0.4 ms

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

- Gemini was **disabled** for this run. All SQL was produced by the deterministic rule-based generator. **This run says nothing about live Gemini performance.**
- Exposure questions (Q9-Q10) are always computed deterministically by the ExposureCalculator; Gemini is never used for financial calculations.

## Limitations

- The ground truth defines 10 questions. A perfect score shows the agent reproduces these answers; it does not prove correctness on unseen questions.
- In deterministic mode the rule-based generator emits SQL equivalent to the official queries for the supported question patterns; questions outside those patterns return `unsupported`.
- Gemini-generated SQL is non-deterministic. Results from `--use-llm` runs can vary between runs and model versions, and depend on API availability (timeouts fall back to rule-based SQL, which is reported per question).
- Comparison ignores column names and tolerates extra columns, but requires all expected values, the exact row count and, where the official query specifies `ORDER BY`, the same row order. Rows tied on the ORDER BY key could legitimately appear in a different order; none of the supplied questions currently rely on this.
- Q7 is evaluated with the official market-value definition (quantity x current_price, as a share of total portfolio value including bonds) and Q8 with the supplied diversification semantics (distinct Stock sectors / all holdings). These are intentionally different from the weight-based exposure calculation used for Q9-Q10.

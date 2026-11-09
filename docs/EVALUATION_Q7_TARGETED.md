# Evaluation Report - PL Capital AI Portfolio Analytics Agent

> **Targeted subset run: Q7 only** (1 of 10 ground-truth questions). This is **not** a full evaluation; its score must not be combined with or presented as a full-run score.

**Subset score: 1/1**

| Item | Value |
|---|---|
| Evaluation mode | Gemini-enabled SQL generation (live API) |
| Questions evaluated | Q7 (subset) |
| Gemini model | `gemini-3.5-flash-lite` |
| Run started | 2026-11-09T04:08:32+05:30 |
| Duration | 2.08 s |
| Python | 3.13.16 (Windows) |
| Ground truth | `ground_truth_dataset.json` (1 questions) |
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
| Q7 | text2sql | hard | sql_query | sql_query | gemini | 8/8 | PASS |

## Failed Questions

None. All questions passed.

## Question Details

### Q7 - PASS

**Question:** For each portfolio, show the total value of Technology sector holdings and what percentage it represents of the total portfolio value

- Selected tool: `sql_query`
- Agent status: `success`
- Generation method: `gemini`
- Model: `gemini-3.5-flash-lite`
- Agent time: 2081.0 ms
- Ordered comparison: yes

Agent SQL:

```sql
WITH portfolio_totals AS (SELECT p.portfolio_id, p.portfolio_name, SUM(h.quantity * s.current_price) AS total_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id GROUP BY p.portfolio_id, p.portfolio_name), tech_holdings AS (SELECT p.portfolio_id, SUM(h.quantity * s.current_price) AS tech_value FROM portfolios p JOIN holdings h ON p.portfolio_id = h.portfolio_id JOIN securities s ON h.security_id = s.security_id JOIN sectors sec ON s.sector_id = sec.sector_id WHERE sec.sector_name = ? GROUP BY p.portfolio_id) SELECT pt.portfolio_name, th.tech_value AS numerator, pt.total_value AS denominator, ROUND((th.tech_value * 100.0) / pt.total_value, 2) AS technology_percentage FROM portfolio_totals pt JOIN tech_holdings th ON pt.portfolio_id = th.portfolio_id ORDER BY technology_percentage DESC
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

| portfolio_name | numerator | denominator | technology_percentage |
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

## Verification Status

- Gemini was enabled and live API requests were attempted with model `gemini-3.5-flash-lite`. 1 of 1 SQL questions were answered with Gemini-generated SQL; 0 used the rule-based fallback (reasons listed per question).
- Exposure questions (Q9-Q10) are always computed deterministically by the ExposureCalculator; Gemini is never used for financial calculations.

## Limitations

- The ground truth defines 10 questions. A perfect score shows the agent reproduces these answers; it does not prove correctness on unseen questions.
- In deterministic mode the rule-based generator emits SQL equivalent to the official queries for the supported question patterns; questions outside those patterns return `unsupported`.
- Gemini-generated SQL is non-deterministic. Results from `--use-llm` runs can vary between runs and model versions, and depend on API availability (timeouts fall back to rule-based SQL, which is reported per question).
- Comparison ignores column names and tolerates extra columns, but requires all expected values, the exact row count and, where the official query specifies `ORDER BY`, the same row order. Rows tied on the ORDER BY key could legitimately appear in a different order; none of the supplied questions currently rely on this.
- Q7 is evaluated with the official market-value definition (quantity x current_price, as a share of total portfolio value including bonds) and Q8 with the supplied diversification semantics (distinct Stock sectors / all holdings). These are intentionally different from the weight-based exposure calculation used for Q9-Q10.

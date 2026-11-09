# Stitch Export: Reconciliation Record

The Stitch HTML and screenshots in `../design-reference/` were used as the **visual** reference. They also
contained invented or incorrect data. This record lists what was changed and why. Every database value below was
read from `database/portfolio.db` (read-only).

## 1. Design-system corrections

| Stitch export | Implementation | Why |
|---|---|---|
| Typeface **Hanken Grotesk** | **Figtree**, self-hosted (`@fontsource-variable/figtree`) | Figtree is PL Capital's real brand font (measured on plindia.com). Loaded fonts were verified in a browser. |
| `font-label-sm`, `font-headline-kpi` … classes (font-family only) | `text-*` utilities carrying size, weight, line-height and tracking | The export applied family-only classes, so its screenshots render headings at body size. The type scale in `DESIGN.md` is what was intended. |
| Tailwind, fonts and icons loaded from CDNs | Built CSS, self-hosted font, Lucide icons | Works offline; a browser audit confirmed the only host contacted is `127.0.0.1:8000`. |
| Google-hosted logo URL (broken in the screenshots) | PL Capital logo SVG shipped in `public/` | Same logo as plindia.com's header. |
| No navigation below the `md` breakpoint | Full-screen mobile menu sheet | Mobile users could not navigate. |
| User avatar in the header | Removed | It implied accounts and authentication, which the prototype does not have. |
| Missing About / How it works screen | Created (`/about`) | Requested. |
| Header crowded at 768 px (page scrolled sideways) | Title, chip and secondary link appear from `lg` upward | Found by the responsive audit and fixed. |

## 2. Statements removed or corrected

| Where | Stitch text | Resolution |
|---|---|---|
| Portfolios footer | "No benchmark data configured" | **False**: all 13 portfolios have a `benchmark_index` code that matches a row in `benchmarks`. The claim is removed. The Benchmark column was left out by choice, not because the data is absent. |
| Portfolios header | `SELECT name, strategy, target_risk, status, aum FROM portfolios` | Real columns are `portfolio_name, strategy_type, target_risk_level, status, total_aum`. The screen shows the real query. |
| Ask: generated SQL | `s.ticker, s.name, h.shares, s.id, p.name, p.id` | Real schema: `symbol, company_name, quantity, security_id, portfolio_name, portfolio_id`. The UI shows the SQL the agent actually generated. |
| Ask pipeline | "Executed in 18ms", "AST parsed & keywords audited" | The backend returns no timing, and validation is lexical plus read-only engine controls, not an AST parse. Step captions now state only what is known. |
| Ask table | Unrealized gain/loss column, "Weighted Return: +3.29%", "Total cost basis $1,435,840" | Not backend fields. The real five rows sum to **$1,535,840**. |
| Exposure header | "Deterministic Rule Engine v2.4" | No such version exists. Removed. |
| Exposure donut | "Excluded (Bonds)" and "Total Sum: 100.0%" | Now **"Excluded from exposure"**, derived as 100% minus the equity weight; the contradictory "total sum" line is gone. |
| Evaluation | "Accuracy: 95% Aggregate", "Run hash: #8fbc29a", "Dataset Baseline: v2.4", "100% Match" | Invented. Removed. Scores come from the saved reports. |
| Evaluation matrix | Questions and numbering that do not match the ground truth (for example "Portfolios with Technology exposure > 50%", "Count of securities by sector") | Questions now come from `ground_truth_dataset.json`; results come from the report files. |
| Portfolios, Performance and Risk | "+18.4% YTD", "Alpha +3.2%", "VaR 95% 2.45%", "Max drawdown −14.2%", "beta 1.34" | Not supported by the data. The schema has **no YTD or alpha field**. −14.2 (max drawdown), 2.45 (VaR 95) and 1.34 (beta) match no stored value. The bare numbers 18.4 and 3.2 do occur in the table, but in unrelated fields (18.4 is a Growth Equity Fund *volatility* reading; 3.2 is a Growth Equity Fund 1-month return and a Conservative Income Fund max drawdown), so the "YTD" and "Alpha" labels are unsupported. The tabs now show the real records. |
| Exposure highlights | Green "4 Equity Assets Evaluated" and "100% Equity Coverage" badges | Green is reserved for gains and success; these are now neutral. |

## 3. Figure verification against the database

**Growth Equity Fund, top 5 by cost basis**

| Symbol | Stitch shares → real | Stitch price → real | Stitch sector → real |
|---|---|---|---|
| AAPL | 2,400 → **2,500** | $189.50 → **$185.50** | Information Technology → **Technology** |
| AMZN | 1,800 → **100** | $178.20 → **$3,380.00** | Consumer Discretionary → same |
| MSFT | 800 → 800 | $415.00 → **$415.26** | Information Technology → **Technology** |
| NVDA | 320 → **400** | $920.00 → **$875.30** | Semiconductors → **Technology** |
| CRM | 600 → **1,000** | $302.10 → **$210.90** | Cloud Applications → **Technology** |

Cost bases ($438,000 · $320,000 · $304,400 · $288,040 · $185,400) were correct. The portfolio holds 8 holdings with a
total cost basis of $1,986,850. The "% of Portfolio" column in the Stitch mock was really each holding's share of the
top five, so it is not shown.

**Balanced Portfolio holdings** (weights from `holdings.current_weight`)

| Stitch | Real |
|---|---|
| MSFT 14%, AAPL 8% | MSFT **10%**, AAPL **12%** |
| JPM 8%, JNJ 6% | JPM 8%, JNJ 6% (correct) |
| Bonds: US10Y 25%, US30Y 15%, LQD 14%, HYG 10% | UST-10Y 25%, UST-5Y 18%, UST-2Y 15%, CORP-AAA-5Y 6% (no LQD, HYG or US30Y in the dataset) |

Equity total 36% and bond total 64% were correct. The table view now lists the real holdings.

**International Equity Fund**: Stitch named "TotalEnergies, ASML, Toyota Motor, Nestlé" as primary holdings. The dataset
is entirely US-listed: Chevron (20%) and Exxon (18%) in Energy, Tesla (15%) in Automotive, Alphabet (12%), Broadcom
(10%), Apple (8%) and Microsoft (6%) in Technology, Home Depot (11%) in Consumer Staples. Sector percentages
(38 / 36 / 15 / 11) were correct, and the UI derives "Largest holding" per sector from the real rows.

**Other**: "10 sectors aggregated" (average price per sector) → the real query groups **7** sectors.

## 4. Behaviour verified in a browser (Chromium, 1440 / 768 / 390 px)

- Every route renders at all three widths with no horizontal page scroll (the 768 px problem above was found and fixed).
- No console errors or warnings, no failed requests, and no request to any host other than `127.0.0.1:8000`.
- The Figtree variable font is the only font loaded for page text.
- None of the removed strings above appear in the rendered text of Ask, Exposure, Portfolios or Evaluation.
- All five suggestion chips return `success` from the real agent (rule-based mode).
- "No data available" appears for the nine portfolios with no performance or risk records; the four with records show them.
- The mobile menu opens and closes after navigation.

Not verified: Gemini-generated answers through this interface. The verification ran with `--no-llm`, so no live API
request was made.

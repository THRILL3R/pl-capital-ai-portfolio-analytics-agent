# PL Capital Portfolio Analytics: Web Interface

A React + Vite + TypeScript interface for the existing Python agent. It is **optional and additive**: the CLI
(`main.py`), the evaluator (`evaluate.py`) and every class in `portfolio_agent/` are unchanged.

| Screen | Route | Source of its data |
|---|---|---|
| Ask | `/ask` | `PortfolioAgent.answer_question`, returned verbatim (including `conversation` replies to greetings) |
| Portfolios | `/portfolios` | Fixed read-only queries through `PortfolioDatabase.execute_read_only` |
| Exposure | `/exposure` | `ExposureCalculator.calculate` (deterministic) plus the portfolio's real holdings |
| Evaluation | `/evaluation` | The evaluator's saved report files (static view) |
| About | `/about` | Static explanatory copy |

Visual reference: `design-reference/` holds the original Stitch exports (HTML, screenshots and `DESIGN.md`)
untouched. See [docs/STITCH_RECONCILIATION.md](docs/STITCH_RECONCILIATION.md) for what was kept, what was
corrected and why.

## Run it

Prerequisites: the project's Python environment (see the root README) and Node.js 20+.

```powershell
# from the project root
cd frontend
npm install
npm run build            # builds frontend/dist
cd ..
.\.venv\Scripts\python.exe -m portfolio_api            # http://127.0.0.1:8000  (UI + API in one process)
.\.venv\Scripts\python.exe -m portfolio_api --no-llm   # rule-based generator only; never calls Gemini
```

`GEMINI_API_KEY` and `GEMINI_MODEL` are read from `.env` by the Python process exactly as the CLI does. They never
reach the browser.

Development with hot reload (two terminals):

```powershell
.\.venv\Scripts\python.exe -m portfolio_api --no-llm   # API on 127.0.0.1:8000
cd frontend; npm run dev                               # UI on http://127.0.0.1:5173, /api proxied to :8000
```

The Python adapter's dependencies (`fastapi`, `uvicorn`, `pydantic`, `httpx`) are already listed in the root
`requirements.txt`. If they are missing from your virtual environment: `pip install fastapi uvicorn pydantic httpx`.

## The integration layer (`portfolio_api/`)

The project had a CLI and no HTTP API, so a minimal local adapter was added. It wraps the existing classes and
adds no business logic.

| Endpoint | Purpose |
|---|---|
| `POST /api/ask` `{question}` | Runs `PortfolioAgent.answer_question`; the response contract (`status, question, selected_tool, route_reason, result, error`) is returned as is. |
| `POST /api/exposure` `{portfolio_name}` | Runs `ExposureCalculator.calculate`; result returned as is. |
| `GET /api/portfolios`, `GET /api/portfolios/{id}` | Portfolio list; holdings, performance and risk records. Fixed, parameterised, read-only queries. |
| `GET /api/evaluation` | Parses `docs/EVALUATION_FINAL_LIVE_20261109.md` (the preferred, final live run), the historical `evaluation_report.md` (earlier 9/10 run, returned separately as `historical_live`), `docs/EVALUATION_DETERMINISTIC.md` and `docs/EVALUATION_Q7_TARGETED.md`, and merges the ground-truth question text. Each report is classified by the mode recorded inside it, so a deterministic run can never appear as live evidence. |
| `GET /api/health` | Booleans and a dataset row count only. |

Safeguards: loopback-only binding (a non-loopback `--host` is refused), `Host` header allow-list, input length
limits, no CORS (single origin), no OpenAPI/docs routes, generic error messages, `Cache-Control: no-store` on
API responses, serialised access to the shared SQLite connection. There is **no authentication**; this is a local
single-user prototype. Do not expose it to a network with real data or credentials. The restricted public demo on
Vercel (Gemini disabled, no secrets, sample data only) is described in the root README.

## Project layout

```
frontend/
├── design-reference/        original Stitch exports (read-only reference)
├── public/                  PL Capital logo (copied from plindia.com's public logo asset)
├── src/
│   ├── api/                 typed client + types mirroring the Python response contracts
│   ├── components/          Layout, ui primitives, DataTable, charts, exposure panels
│   ├── lib/                 formatting, CSV export, exposure helpers (pure, unit-tested)
│   ├── pages/               Ask, Portfolios, Exposure, Evaluation, About (+ tests)
│   └── test/setup.ts
├── tailwind.config.ts       design tokens from Stitch DESIGN.md (typeface changed to Figtree)
└── vite.config.ts           dev proxy + Vitest config
```

## Scripts

| Command | What it does |
|---|---|
| `npm run dev` | Vite dev server with `/api` proxy |
| `npm run build` | Typecheck, then production build into `dist/` |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | 94 Vitest tests (client, helpers, SQL formatting, all five screens) |

## Data and honesty rules the UI follows

- No figure is hardcoded. Holdings, prices, weights, AUM, performance and risk come from the database; evaluation
  scores come from the saved reports.
- The table on the Ask screen renders whatever columns the agent returned. There is no computed P&L, "weighted
  return", latency or accuracy figure, because the backend does not provide them.
- The Query Details panel shows only fields that exist in the response (`status`, `selected_tool`, `route_reason`,
  `generation_method`, `model_name`, `fallback_used` and `fallback_reason`, `row_count`, `query_description`).
- The Ask screen is a chat workspace: conversation history (oldest first) scrolls on its own above a composer pinned
  to the bottom; the inspector panels sit beside it on desktop and open from a **Details** button on smaller screens.
  A submitted question scrolls into view, and a finished answer only re-scrolls if the reader is still at the bottom.
- Generated SQL is formatted **for display only** (`src/lib/sql.ts`, `SqlCode.tsx`): 4-space indentation, one
  column / `FROM` / `JOIN` / clause per line, `ON` indented under its `JOIN`, role-based colours, and a hanging
  indent so wrapped lines on narrow screens stay aligned. The formatter's output must equal the original SQL
  once whitespace is removed, otherwise the original text is shown. **Copy SQL** copies the original string from
  the response, and bound parameters are untouched (hover a `?` to see its value).
- Performance and risk values are shown **as stored**: the schema declares them as `REAL` and documents no units.
- Green is reserved for gains and success (for example PASS). Sector charts use the indigo/violet categorical palette.
- A fallback is always labelled "Fallback used" and is never presented as Gemini output.
- The donut's unfilled remainder is labelled **"Excluded from exposure"**. It is derived as 100% minus the total
  equity weight, because the exposure result carries no bond weight.

## Known limitations

- No authentication, by design; localhost only.
- Gemini answers are non-deterministic and not covered by any automated test (tests never make live API calls).
- The Stitch "Welcome / entry" screen is not implemented: it was not in the export, and the prototype has no login.
- Result-column units are inferred from column *names* (for example `*_aum`, `*_price`, `*_pct`). Unrecognised names
  are shown as plain numbers rather than guessing a unit.
- Icons use Lucide (outline) instead of Material Symbols, to avoid an external font dependency.

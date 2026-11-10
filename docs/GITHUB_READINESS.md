# GitHub Readiness and Security Review

Review date: 2026-11-09. No secret values are reproduced in this document.

## Publication status: **READY FROM THE REPOSITORY SIDE. One manual step remains: revoke the old key.**

Updated 2026-11-09:

- **Done:** `.env.example` now contains placeholders only (`GEMINI_API_KEY=your_gemini_api_key_here`,
  `ONEMIN_API_KEY=your_1min_api_key_here`).
- **Verified:** a project-wide scan finds real credentials only in the git-ignored `.env`. A
  `.gitignore` check confirms `.env` is ignored and `.env.example` is tracked.
- **Remaining, manual:** the `GEMINI_API_KEY` value that was previously written into `.env.example`
  must be treated as exposed and **revoked / rotated in Google AI Studio**. No Git repository exists
  yet, so it was never committed or pushed. Rotation is still required before publishing. Confirm it,
  then continue with [§6](#6-manual-steps-before-publication) step 3 onward.

## 1. Security review findings

The scan reported only file names; no matched values were printed.

| # | Check | Result | Severity / action |
|---|---|---|---|
| S1 | Real credential in a **tracked** file | **Resolved 2026-11-09.** `.env.example` had held a non-placeholder `GEMINI_API_KEY` value; it now holds placeholders only (`ONEMIN_API_KEY` placeholder added). `ONEMIN_API_KEY` was never present in `.env.example`. | Placeholder restored. **Old `GEMINI_API_KEY` must still be revoked manually.** |
| S2 | Credential in an **ignored** file | `.env` contains `GEMINI_API_KEY` and `ONEMIN_API_KEY`. | Expected. `.env` is git-ignored (verified with `git check-ignore`). |
| S3 | Hardcoded keys in source code | None in `portfolio_agent/`, `main.py`, `init_db.py`, `evaluate.py` or `scripts/`. | OK |
| S4 | Key-shaped strings in tests | `tests/test_sql_generator.py` contains `FAKE_KEY`, an obviously fake value used to test redaction. It is shorter than the Google API-key pattern used by secret scanners. | OK (intentional test fixture) |
| S5 | Other secrets (passwords, tokens, private keys) | None found. | OK |
| S6 | Key exposure through logs / output | Error text is redacted (key value, `key=` URL parameters, `AIza…` patterns). The generator's `repr` hides the key. The smoke test prints only `present` / `MISSING`. | OK |
| S7 | Sensitive logging | Failed SQL statements are logged at ERROR level. They contain no secrets. No prompts, keys or environment values are logged. | OK |
| S8 | Unsafe SQL execution | Every query path runs validation plus three engine-level controls (`mode=ro`, `query_only`, authorizer). Gemini SQL is validated before execution. Interpolated identifiers come from internal or schema-checked sources only. | OK |
| S9 | Generated artefacts | `database/portfolio.db` (about 213 KB) is git-ignored and rebuilt by `init_db.py`. `.venv/` and `__pycache__/` are ignored. | OK |
| S10 | Automated tests reaching the live API | Not possible: `tests/__init__.py` clears API keys and disables `.env` loading for the test process and CLI subprocesses. | OK |
| S11 | `.gitignore` coverage | Was missing `.env.*` variants, `*.log` and IDE folders. **Fixed in Phase 7.** | Fixed |

## 2. `.gitignore` verification

| Path / pattern | Expected | Rule |
|---|---|---|
| `.env` | ignored | `.env`, `*.env` |
| `.env.local`, `.env.production` | ignored | `.env.*` (added) |
| `.env.example` | **tracked** — placeholders only | `!.env.example` |
| `.venv/`, `venv/`, `env/` | ignored | virtual environments |
| `__pycache__/`, `*.pyc`, `.pytest_cache/` | ignored | caches |
| `database/*.db`, `-journal`, `-wal`, `-shm`, `*.tmp` | ignored | generated database |
| `*.log` | ignored | added |
| `.vscode/`, `.idea/`, `.claude/` | ignored | added |
| `evaluation_report.md` | **tracked** | historical earlier live run (9/10), labelled as such; the final live result is `docs/EVALUATION_FINAL_LIVE_20261109.md` (10/10) |
| `evaluation_report_subset.md` | not needed | default output of `--questions` subset runs; publish only if intended |

After `git init`, confirm with:

```powershell
git check-ignore -v .env .venv database/portfolio.db   # each should print the matching rule
git check-ignore -v .env.example                       # should print nothing (tracked)
git add --dry-run .                                    # review the exact file list before committing
```

## 3. Repository metadata (suggested)

- **Name:** `pl-capital-portfolio-analytics-agent`
- **Description:** "CLI AI agent for portfolio analytics: Gemini-powered text-to-SQL with deterministic
  fallback, equity sector-exposure calculator, read-only SQLite safety layers and a ground-truth
  evaluator (PL Capital AI/ML assessment)."
- **Topics:** `python`, `sqlite`, `text-to-sql`, `gemini-api`, `google-genai`, `llm-agent`,
  `portfolio-analytics`, `fintech`, `sql-safety`, `evaluation`, `cli`

## 4. Files to include

```
README.md
main.py
evaluate.py
init_db.py
evaluation_report.md
requirements.txt              (official, unchanged)
database_schema.sql           (official, unchanged)
ground_truth_dataset.json     (official, unchanged)
.env.example                  (placeholders ONLY)
.gitignore
data/*.csv                    (9 official datasets, unchanged)
portfolio_agent/*.py
scripts/gemini_smoke_test.py
tests/*.py                    (596 tests, incl. test_prompt_conventions.py, test_api.py, test_auth.py, test_conversation.py and test_intent_classifier.py)
docs/ORIGINAL_ASSESSMENT_README.md
docs/PROJECT_OVERVIEW.md
docs/GITHUB_READINESS.md
docs/EVALUATION_DETERMINISTIC.md
docs/EVALUATION_Q7_TARGETED.md
docs/PROMPT_IMPROVEMENT_REPORT.md
```

Optional benchmark records: `docs/GEMINI_MODEL_BENCHMARK.md`, `docs/GEMINI_MODEL_RESULTS.csv`,
`docs/ARGON_*.md` / `.csv`, plus their scripts (`scripts/test_all_gemini_models.py`,
`scripts/test_argon_*.py`, `scripts/test_onemin_argon.py`). They document model availability and the
1min.AI Argon trials. Review them before publishing; they contain no keys, as confirmed by the
file-name-only scan in §6.

Optional: the candidate-prepared BRD / PRD / TRD PDFs, which currently sit outside the project folder.
Add them under `docs/` only if you want reviewers to see them.

## 5. Files to exclude

`.env` · `.venv/` · `database/portfolio.db` (and journal/WAL/tmp files) · `__pycache__/` · `*.pyc` ·
logs · IDE folders · the original assessment ZIP · any local evaluation reports you don't intend to
publish (e.g. `evaluation_report_subset.md` from a targeted run).

## 6. Manual steps before publication

1. ~~**Restore the placeholder** in `.env.example`.~~ **Done (2026-11-09):** placeholders only.
2. **Rotate the API key** in Google AI Studio: create a new key, delete the old one, and put the new key
   **only** in `.env`. The old key was written to a file that is meant to be published, so treat it as
   exposed. *(Still to do; manual.)*
3. **Re-run the secret-safe scan.** It prints file names only. The only expected hit is `.env`:
   ```powershell
   Get-ChildItem -Recurse -File -Force | Where-Object { $_.FullName -notmatch '\\(\.venv|__pycache__)\\' -and $_.Extension -ne '.db' } |
     Select-String -Pattern 'AQ\.[0-9A-Za-z_\-]{20,}','AIza[0-9A-Za-z_\-]{35}','GEMINI_API_KEY\s*=\s*(?!your_gemini_api_key_here)\S{12,}' -List |
     Select-Object -ExpandProperty Path
   ```
4. **Live Gemini status is already recorded** in the README: the final full live run scored **10/10**
   (`gemini-3.5-flash-lite`, Q1–Q8 Gemini-generated SQL, 0 fallbacks, Q9–Q10 deterministic;
   `docs/EVALUATION_FINAL_LIVE_20261109.md`). The earlier 9/10 run and the targeted Q7 re-test are kept as
   historical records. **After rotating the key**, re-run the smoke test or a live evaluation to confirm the
   new key works. Update the README only with results that were actually obtained.
5. Run the final checks:
   `python -B -m unittest discover -s tests -t .` (expect 596 OK: 0 failures and 0 skipped in the project venv with all requirements installed) and
   `.\.venv\Scripts\python.exe evaluate.py --report docs\EVALUATION_DETERMINISTIC.md` (expect 10/10,
   deterministic). Use `--report` so the deterministic run does not overwrite the live
   `evaluation_report.md`.
6. `git init` → the `.gitignore` checks in §2 → `git add .` → review `git status` → commit → create the
   GitHub repository → push.
7. After pushing, open the repository's file list on GitHub and confirm that `.env`, `.venv/` and
   `database/portfolio.db` are absent and that `.env.example` shows the placeholder.

## 7. Final security checklist

- [x] `.env.example` contains placeholders only (2026-11-09)
- [ ] Old `GEMINI_API_KEY` rotated / deleted in Google AI Studio
- [x] Secret-safe scan finds credentials only in `.env` (2026-11-09; plus the intentional `FAKE_KEY` test fixture)
- [ ] `git check-ignore` confirms `.env`, `.venv/`, `database/*.db` are ignored
- [ ] `git add --dry-run .` lists only the files in §4
- [ ] 596 unit tests pass (project venv); deterministic evaluator reports 10/10
- [ ] README distinguishes the deterministic 10/10, the final live 10/10 (`docs/EVALUATION_FINAL_LIVE_20261109.md`) and the historical 9/10 run
- [ ] README Gemini status matches the latest real live results
- [ ] No claims of live Gemini verification beyond what was actually run

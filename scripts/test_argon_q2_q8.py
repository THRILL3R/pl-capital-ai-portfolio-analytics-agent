#!/usr/bin/env python3
"""
gemini-4-argon text-to-SQL evaluation on official questions Q2-Q8 (1min.AI gateway).

- ONLY ONEMIN_API_KEY + https://api.1min.ai/api/chat-with-ai; GEMINI_API_KEY is not used.
- Exactly one generation request per question, sequential, no retries. Stops on a
  credit / quota / auth error and marks the remaining questions as not run.
- Prompt = the same template as scripts/test_argon_portfolio.py (Q1): live schema
  introspected from database/portfolio.db + the question; no answer hints.
- Model SQL is validated with validate_read_only_sql() and executed only through
  PortfolioDatabase.execute_read_only() (mode=ro, query_only, authorizer).
- Expected results come from the official ground-truth SQL, executed by the existing
  evaluator's run_expected_sql(); comparison uses the evaluator's compare_tables()
  (abs 1e-6 / rel 1e-9 tolerance, ordered when the official SQL has ORDER BY).
- Q9-Q10 are not tested (deterministic ExposureCalculator questions).

Usage:  .venv\\Scripts\\python.exe scripts\\test_argon_q2_q8.py
Writes: docs/ARGON_Q2_Q8_EVALUATION.md
Exit codes: 0 = 7/7, 1 = otherwise, 2 = not configured.
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from evaluate import ABS_TOL, REL_TOL, compare_tables, load_ground_truth, run_expected_sql  # noqa: E402
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError, validate_read_only_sql  # noqa: E402
from test_argon_portfolio import ENDPOINT, MODEL_ID, extract_sql, load_onemin_key, redact  # noqa: E402

QUESTION_IDS = range(2, 9)
REQUEST_TIMEOUT_S = 300
REPORT_PATH = PROJECT_ROOT / "docs" / "ARGON_Q2_Q8_EVALUATION.md"
GROUND_TRUTH = PROJECT_ROOT / "ground_truth_dataset.json"


def build_prompt(schema: str, question: str) -> str:
    """Same template as scripts/test_argon_portfolio.py (Q1), with the question substituted."""
    return (
        "You are a text-to-SQL engine for a SQLite portfolio analytics database.\n"
        "Use ONLY the tables and columns in the schema below.\n\n"
        f"{schema}\n\n"
        f"Question: {question}\n\n"
        "Return ONLY one valid, read-only SQLite SELECT statement. "
        "No explanation, no comments, no markdown, no code fences."
    )


def secs(value) -> str:
    return f"{value:.2f}s" if value is not None else "—"


class StopRun(Exception):
    """Credits, quota or authentication problem: do not send further requests."""


def ask_argon(api_key: str, prompt: str) -> tuple[str, float]:
    started = time.perf_counter()
    try:
        resp = requests.post(
            ENDPOINT,
            headers={"API-KEY": api_key, "Content-Type": "application/json"},
            json={"type": "UNIFY_CHAT_WITH_AI", "model": MODEL_ID, "promptObject": {"prompt": prompt}},
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.Timeout:
        raise RuntimeError(f"timeout after {time.perf_counter() - started:.1f}s")
    except requests.RequestException as exc:
        raise RuntimeError(f"{type(exc).__name__}: {redact(exc, api_key)[:200]}")
    elapsed = time.perf_counter() - started
    if not resp.ok:
        body = redact(resp.text, api_key)[:300]
        if resp.status_code in (401, 402, 403, 429) or re.search(r"credit|insufficient|balance|quota", body, re.I):
            raise StopRun(f"HTTP {resp.status_code}: {body}")
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("response is not JSON")
    result = ((data.get("aiRecord") or {}).get("aiRecordDetail") or {}).get("resultObject")
    reply = result[0] if isinstance(result, list) and result else result
    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError(f"no text in response (keys: {list(data.keys())})")
    return reply, elapsed


def fmt(v) -> str:
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".")
    return "NULL" if v is None else str(v)


def md_table(columns, rows, limit=15) -> str:
    if not columns:
        return "_(none)_"
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(fmt(x).replace("|", "\\|") for x in r) + " |")
    if len(rows) > limit:
        out.append(f"\n_… {len(rows) - limit} more rows_")
    return "\n".join(out)


def main() -> int:
    api_key = load_onemin_key()
    print(f"ONEMIN_API_KEY: {'present' if api_key else 'MISSING'} | model {MODEL_ID} | {ENDPOINT}")
    if not api_key:
        return 2
    db = PortfolioDatabase()
    if not db.is_initialized():
        print("Database not initialised. Run: python init_db.py")
        return 1
    schema = db.table_schema()
    questions = {int(q["id"]): q for q in load_ground_truth(GROUND_TRUTH)}

    results = []
    stopped = None
    for qid in QUESTION_IDS:
        q = questions[qid]
        gt_sql = q["ground_truth"]["sql_query"]
        r = {"id": qid, "question": q["question"], "difficulty": q.get("difficulty", ""),
             "official_sql": gt_sql, "ordered": "ORDER BY" in gt_sql.upper(),
             "reply": "", "sql": "", "latency": None, "safety": "not run", "execution": "not run",
             "exp_cols": [], "exp_rows": [], "act_cols": [], "act_rows": [], "verdict": "NOT RUN",
             "failures": [], "notes": []}
        r["exp_cols"], r["exp_rows"] = run_expected_sql(db.db_path, gt_sql)
        results.append(r)
        if stopped:
            r["failures"].append(f"Not run: stopped after credit/quota/auth error ({stopped[:120]}).")
            continue

        print(f"Q{qid} ... ", end="", flush=True)
        try:
            r["reply"], r["latency"] = ask_argon(api_key, build_prompt(schema, q["question"]))
        except StopRun as exc:
            stopped = str(exc)
            r["execution"] = "api error"
            r["failures"].append(f"API stop condition: {exc}")
            print(f"STOP ({str(exc)[:80]})")
            continue
        except RuntimeError as exc:
            r["execution"] = "api error"
            r["verdict"] = "FAIL"
            r["failures"].append(f"API error: {exc}")
            print(f"API error ({str(exc)[:80]})")
            continue

        r["sql"] = extract_sql(r["reply"])
        try:
            cleaned = validate_read_only_sql(r["sql"])
            r["safety"] = "ACCEPTED"
        except ReadOnlyQueryError as exc:
            r["safety"] = f"REJECTED: {exc}"
            r["verdict"] = "FAIL"
            r["failures"].append("SQL rejected by safety validation; not executed.")
            print(f"{r['latency']:.2f}s REJECTED")
            continue
        try:
            out = db.execute_read_only(cleaned)
            r["execution"] = "success"
        except ReadOnlyQueryError as exc:
            r["execution"] = f"error: {exc}"
            r["verdict"] = "FAIL"
            r["failures"].append("Execution error.")
            print(f"{r['latency']:.2f}s EXEC ERROR")
            continue
        r["act_cols"] = out["columns"]
        r["act_rows"] = [tuple(row.get(c) for c in out["columns"]) for row in out["rows"]]
        ok, failures, notes = compare_tables(r["exp_cols"], r["exp_rows"], r["act_cols"], r["act_rows"], r["ordered"])
        r["verdict"] = "PASS" if ok else "FAIL"
        r["failures"] += failures
        r["notes"] += notes
        print(f"{r['latency']:.2f}s {r['verdict']}")
    db.close()

    passed = sum(r["verdict"] == "PASS" for r in results)
    REPORT_PATH.write_text(render(results, passed, stopped), encoding="utf-8")
    print(f"\nScore: {passed}/{len(results)}  ->  {REPORT_PATH}")
    return 0 if passed == len(results) else 1


def render(results, passed, stopped) -> str:
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lat = [r["latency"] for r in results if r["latency"] is not None]
    L = ["# gemini-4-argon — Text-to-SQL Evaluation, Q2–Q8\n",
         f"Run: {run_at} · Model `{MODEL_ID}` via the 1min.AI gateway (`{ENDPOINT}`) · "
         "Generated by `scripts/test_argon_q2_q8.py`. No API key appears in this report.\n",
         "## Method\n",
         "- One generation request per question, sequential, no retries "
         f"({len(lat)} requests sent{'; run stopped early: ' + stopped[:120] if stopped else ''}).",
         "- Prompt: the same template as the Q1 test — the live schema introspected from `database/portfolio.db` "
         "plus the question, asking for one read-only SQLite SELECT only. **No domain hints or expected answers "
         "were given.** (The production `GeminiSQLGenerator` prompt additionally contains financial-semantics "
         "rules and value hints; this test does not use them.)",
         "- Safety: `validate_read_only_sql()` → `PortfolioDatabase.execute_read_only()` "
         "(`mode=ro`, `PRAGMA query_only`, SQLite authorizer). Rejected SQL is never executed.",
         "- Expected results: the official `ground_truth_dataset.json` SQL, run unchanged by the evaluator's "
         "`run_expected_sql()` on an independent read-only connection.",
         f"- Comparison: the evaluator's `compare_tables()` — numeric tolerance abs {ABS_TOL:g} / rel {REL_TOL:g}; "
         "column names may differ and extra columns are ignored; row order must match when the official SQL has "
         "`ORDER BY`.",
         "- Q1 was run separately with `scripts/test_argon_portfolio.py` (PASS, 13, 4.71s). Q9–Q10 are "
         "ExposureCalculator questions and were not sent to Argon.\n",
         "## Summary\n",
         f"**Score: {passed}/{len(results)}** (Q2–Q8, evaluator rules)"
         + (f" · latency min {min(lat):.2f}s / median {sorted(lat)[len(lat) // 2]:.2f}s / max {max(lat):.2f}s" if lat else "")
         + "\n",
         "| Q | Difficulty | Safety | Execution | Rows exp / act | Ordered check | Latency | Verdict |",
         "|---|---|---|---|---|---|---:|---|"]
    for r in results:
        L.append(f"| Q{r['id']} | {r['difficulty']} | {r['safety'].split(':')[0]} | {r['execution'].split(':')[0]} | "
                 f"{len(r['exp_rows'])} / {len(r['act_rows']) if r['execution'] == 'success' else '—'} | "
                 f"{'yes' if r['ordered'] else 'no'} | "
                 f"{secs(r['latency'])} | **{r['verdict']}** |")
    L.append("")
    for r in results:
        L.append(f"## Q{r['id']} — {r['question']}\n")
        L.append(f"**Verdict: {r['verdict']}** · Safety: {r['safety']} · Execution: {r['execution']} · "
                 f"Latency: {secs(r['latency'])}\n")
        L.append("Argon SQL:\n\n```sql\n" + (r["sql"] or "(none)") + "\n```\n")
        if r["reply"].strip() != r["sql"].strip():
            L.append("Raw reply (differs from extracted SQL):\n\n```\n" + r["reply"].strip() + "\n```\n")
        L.append("Official SQL:\n\n```sql\n" + r["official_sql"] + "\n```\n")
        L.append("Expected result (official SQL):\n\n" + md_table(r["exp_cols"], r["exp_rows"]) + "\n")
        if r["execution"] == "success":
            L.append("Actual result (Argon SQL):\n\n" + md_table(r["act_cols"], r["act_rows"]) + "\n")
        for f in r["failures"]:
            L.append(f"- ❌ {f}")
        for n in r["notes"]:
            L.append(f"- ℹ️ {n}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())

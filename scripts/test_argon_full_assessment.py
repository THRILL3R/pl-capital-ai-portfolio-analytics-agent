#!/usr/bin/env python3
"""
Full Q1-Q10 PL Capital assessment with gemini-4-argon (1min.AI gateway) on Q1-Q8.

Q1-Q8 (text-to-SQL) - production path, unmodified:
  * The production GeminiSQLGenerator is used as-is. Only its *client* is replaced by a
    small adapter (OneMinGeminiAdapter) that forwards the single generate_content() call
    to https://api.1min.ai/api/chat-with-ai with model gemini-4-argon and ONEMIN_API_KEY.
  * Prompt = production build_system_instruction() (live schema + FINANCIAL_SEMANTICS +
    categorical value hints + strict rules) + the production RESPONSE_JSON_SCHEMA, followed
    by the production user prompt "Question: ...". 1min.AI exposes a single prompt field,
    so the system instruction and the JSON schema are sent as text in that field.
  * No expected answers or official SQL are included in the prompt.
  * The response is parsed by the production parse_gemini_response() (JSON shape,
    parameter types, placeholder count, validate_read_only_sql()); accepted SQL runs only
    through PortfolioDatabase.execute_read_only(sql, params).
  * Expected results: official ground-truth SQL via evaluate.run_expected_sql(); comparison
    via evaluate.compare_tables() (abs 1e-6 / rel 1e-9, ORDER BY => ordered).
  * Exactly one request per question, sequential, no retries; stops on credit / quota /
    auth errors. GEMINI_API_KEY is never used; no key is ever printed.

Q9-Q10 (exposure) - deterministic, no Argon requests:
  * PortfolioAgent (Gemini disabled) -> ExposureCalculator, judged by the existing
    evaluate.evaluate_question() against values computed independently from the CSVs.

Usage:   .venv\\Scripts\\python.exe scripts\\test_argon_full_assessment.py [--questions 4,5,6,7,8] [--preflight]
  (default)      send Q1-Q8 to Argon, run Q9-Q10 deterministically.
  --questions    send ONLY these SQL question IDs to Argon. Other SQL questions are carried
                 over from the previous results CSV: their recorded SQL is re-validated and
                 re-executed locally (no API request). Without a recorded result they are NOT EVALUATED.
  --preflight    one minimal probe request first; abort without sending questions if the
                 gateway does not return usable text.
Outputs: docs/ARGON_FULL_ASSESSMENT_EVALUATION.md, docs/ARGON_FULL_ASSESSMENT_RESULTS.csv
Exit:    0 = 10/10, 1 = otherwise, 2 = not configured, 3 = preflight failed (nothing written).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from evaluate import (  # noqa: E402
    ABS_TOL,
    EXPOSURE_TOL_PP,
    REL_TOL,
    compare_tables,
    compute_expected_exposure,
    evaluate_question,
    load_ground_truth,
    run_expected_sql,
)
from portfolio_agent.agent import PortfolioAgent  # noqa: E402
from portfolio_agent.database import PortfolioDatabase, ReadOnlyQueryError, validate_read_only_sql  # noqa: E402
from portfolio_agent.sql_generator import GeminiGenerationError, GeminiSQLGenerator  # noqa: E402
from portfolio_agent.tools import ExposureCalculator, SQLQueryTool  # noqa: E402
from test_argon_portfolio import ENDPOINT, MODEL_ID, load_onemin_key, redact  # noqa: E402

DATA_DIR = PROJECT_ROOT / "data"
GROUND_TRUTH = PROJECT_ROOT / "ground_truth_dataset.json"
MD_PATH = PROJECT_ROOT / "docs" / "ARGON_FULL_ASSESSMENT_EVALUATION.md"
CSV_PATH = PROJECT_ROOT / "docs" / "ARGON_FULL_ASSESSMENT_RESULTS.csv"
REQUEST_TIMEOUT_S = 300
STOP_CATEGORIES = {"quota", "auth"}
# Mixed stock/bond portfolio used for a supplementary real-data bond-exclusion check.
BOND_CHECK_PORTFOLIO = "Balanced Portfolio"

# Previous Argon runs (schema-only prompt, plain SQL output) - from
# scripts/test_argon_portfolio.py and docs/ARGON_Q2_Q8_EVALUATION.md.
PREVIOUS = {
    1: ("PASS", 4.71, ""),
    2: ("PASS", 3.20, ""),
    3: ("PASS", 4.54, ""),
    4: ("PASS", 5.89, ""),
    5: ("FAIL", 3.67, "omitted holding_id"),
    6: ("FAIL", 6.32, "no ORDER BY"),
    7: ("FAIL", 4.87, "13 rows incl. 0% portfolios, no ORDER BY"),
    8: ("FAIL", 4.62, "own metrics; no official diversification ratio"),
}


# ───────────────────────────────────────────────────────────── 1min.AI adapter


class OneMinAPIError(Exception):
    """Gateway error carrying an HTTP ``code`` so the production classifier can categorise it."""

    def __init__(self, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.code = code


class OneMinGeminiAdapter:
    """Minimal stand-in for google.genai.Client: client.models.generate_content(...)."""

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key
        self.models = self
        self.calls = 0
        self.last_latency: Optional[float] = None
        self.last_raw: str = ""
        self.gateway_empty = False
        self.last_gateway_diag = ""

    def __repr__(self) -> str:
        return "OneMinGeminiAdapter(model=%r)" % MODEL_ID

    def probe(self) -> tuple:
        """One minimal request to confirm the gateway returns usable text. Returns (ok, latency, detail)."""
        self.probe_calls = getattr(self, "probe_calls", 0) + 1
        started = time.perf_counter()
        try:
            resp = requests.post(
                ENDPOINT,
                headers={"API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"type": "UNIFY_CHAT_WITH_AI", "model": MODEL_ID,
                      "promptObject": {"prompt": "Reply with exactly: OK"}},
                timeout=60,
            )
        except requests.RequestException as exc:
            return False, time.perf_counter() - started, f"{type(exc).__name__}: {redact(exc, self._api_key)[:200]}"
        latency = time.perf_counter() - started
        if not resp.ok:
            return False, latency, f"HTTP {resp.status_code}: {redact(resp.text, self._api_key)[:300]}"
        try:
            data = resp.json()
        except ValueError:
            return False, latency, "response is not JSON"
        record = data.get("aiRecord") or {}
        result = (record.get("aiRecordDetail") or {}).get("resultObject")
        text = result[0] if isinstance(result, list) and result else result
        if isinstance(text, str) and text.strip():
            return True, latency, f"HTTP 200, text {text.strip()[:40]!r}, record status {record.get('status')!r}"
        diag = {k: record.get(k) for k in ("status", "message", "error") if k in record}
        diag.update({k: data.get(k) for k in ("message", "error", "code") if k in data})
        return False, latency, ("HTTP 200 with no content; diagnostics "
                                f"{redact(json.dumps(diag, default=str), self._api_key)[:300]}; keys {list(data.keys())}")

    def generate_content(self, model: str, contents: str, config: Dict[str, Any]) -> Any:
        self.calls += 1
        prompt = (
            f"{config['system_instruction']}\n\n"
            "OUTPUT FORMAT: respond with ONLY a JSON object (no markdown, no code fences) that "
            "conforms to this JSON schema:\n"
            f"{json.dumps(config['response_json_schema'], indent=2)}\n\n"
            f"{contents}"
        )
        started = time.perf_counter()
        self.last_latency, self.last_raw = None, ""
        try:
            resp = requests.post(
                ENDPOINT,
                headers={"API-KEY": self._api_key, "Content-Type": "application/json"},
                json={"type": "UNIFY_CHAT_WITH_AI", "model": MODEL_ID, "promptObject": {"prompt": prompt}},
                timeout=REQUEST_TIMEOUT_S,
            )
        except requests.Timeout as exc:
            self.last_latency = time.perf_counter() - started
            raise TimeoutError(f"1min.AI timeout after {self.last_latency:.1f}s") from None
        except requests.RequestException as exc:
            self.last_latency = time.perf_counter() - started
            raise ConnectionError(f"{type(exc).__name__}: {redact(exc, self._api_key)[:200]}") from None
        self.last_latency = time.perf_counter() - started
        if not resp.ok:
            body = redact(resp.text, self._api_key)[:300]
            code = resp.status_code
            if re.search(r"credit|insufficient|balance|quota", body, re.I) and code not in (401, 403):
                code = 429  # treat credit exhaustion as a quota stop condition
            raise OneMinAPIError(f"HTTP {resp.status_code}: {body}", code=code)
        try:
            data = resp.json()
        except ValueError:
            raise OneMinAPIError("1min.AI response is not JSON") from None
        record = data.get("aiRecord") or {}
        result = (record.get("aiRecordDetail") or {}).get("resultObject")
        text = result[0] if isinstance(result, list) and result else result
        self.last_raw = text if isinstance(text, str) else ""
        if not self.last_raw.strip():
            # HTTP 200 without content: a gateway-side refusal (e.g. credits / rate limit), not a
            # model answer. Record redacted diagnostics and make the caller stop the run.
            self.gateway_empty = True
            diag = {k: record.get(k) for k in ("status", "message", "error") if k in record}
            diag.update({k: data.get(k) for k in ("message", "error", "code") if k in data})
            self.last_gateway_diag = redact(json.dumps(diag, default=str), self._api_key)[:300]
            raise OneMinAPIError(
                f"1min.AI returned HTTP 200 with no content (diagnostics: {self.last_gateway_diag}; "
                f"top-level keys: {list(data.keys())})"
            )
        return SimpleNamespace(text=self.last_raw)


# ───────────────────────────────────────────────────────────── helpers


def fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.6f}".rstrip("0").rstrip(".")
    return "NULL" if v is None else str(v)


def md_table(columns, rows, limit=15) -> str:
    if not columns:
        return "_(none)_"
    out = ["| " + " | ".join(map(str, columns)) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        out.append("| " + " | ".join(fmt(x).replace("|", "\\|") for x in r) + " |")
    if len(rows) > limit:
        out.append(f"\n_… {len(rows) - limit} more rows_")
    return "\n".join(out)


def secs(v: Optional[float]) -> str:
    return f"{v:.2f}s" if v is not None else "—"


# ───────────────────────────────────────────────────────────── Q1-Q8


def _new_sql_result(q, db, agent) -> Dict[str, Any]:
    gt_sql = q["ground_truth"]["sql_query"]
    r: Dict[str, Any] = {
        "id": int(q["id"]), "type": q["type"], "difficulty": q.get("difficulty", ""), "question": q["question"],
        "expected_tool": "sql_query",
        "routed_tool": "exposure_calculator" if agent._is_exposure_question(q["question"]) else "sql_query",
        "official_sql": gt_sql, "ordered": "ORDER BY" in gt_sql.upper(), "source": "this run",
        "generation": "not run", "latency": None, "raw": "", "parse": "not run", "safety": "not run",
        "execution": "not run", "sql": "", "params": [], "description": "",
        "act_cols": [], "act_rows": [], "verdict": "NOT EVALUATED", "failures": [], "notes": [],
    }
    r["exp_cols"], r["exp_rows"] = run_expected_sql(db.db_path, gt_sql)
    return r


def _validate_execute_compare(r: Dict[str, Any], db) -> Dict[str, Any]:
    """Explicit validate_read_only_sql re-check -> execute_read_only -> evaluator comparison."""
    try:
        cleaned = validate_read_only_sql(r["sql"])
        r["safety"] = "ACCEPTED"
    except ReadOnlyQueryError as exc:
        r["safety"] = f"REJECTED: {exc}"
        r["verdict"] = "FAIL"
        r["failures"].append("SQL rejected by safety validation; not executed.")
        return r
    try:
        res = db.execute_read_only(cleaned, tuple(r["params"]) or None)
        r["execution"] = "success"
    except ReadOnlyQueryError as exc:
        r["execution"] = f"error: {exc}"
        r["verdict"] = "FAIL"
        r["failures"].append("Execution error.")
        return r
    r["act_cols"] = res["columns"]
    r["act_rows"] = [tuple(row.get(c) for c in res["columns"]) for row in res["rows"]]
    ok, failures, notes = compare_tables(r["exp_cols"], r["exp_rows"], r["act_cols"], r["act_rows"], r["ordered"])
    if r["routed_tool"] != r["expected_tool"]:
        ok = False
        failures.append(f"Agent would route this question to {r['routed_tool']}.")
    r["verdict"] = "PASS" if ok else "FAIL"
    r["failures"] += failures
    r["notes"] += notes
    return r


def carry_over_sql_question(q, db, agent, prev: Optional[Dict[str, str]]) -> Dict[str, Any]:
    """Re-check a previously recorded Argon answer locally (no API request)."""
    r = _new_sql_result(q, db, agent)
    if not prev or not (prev.get("generated_sql") or "").strip():
        r["source"] = "not selected"
        r["failures"].append("Not selected for this run and no recorded Argon SQL to carry over.")
        return r
    r["source"] = "carried over"
    r["generation"] = "success (recorded in previous run; not re-sent)"
    r["parse"] = f"accepted (previous run: {prev.get('parse_validation', '?')})"
    r["latency"] = float(prev["latency_s"]) if prev.get("latency_s") else None
    r["sql"] = prev["generated_sql"]
    r["params"] = json.loads(prev.get("parameters") or "[]")
    r["description"] = prev.get("model_description", "")
    r["notes"].append("Carried over: recorded SQL re-validated and re-executed locally; no new API request.")
    return _validate_execute_compare(r, db)


def run_sql_question(q, db, generator, adapter, agent, stopped: Optional[str]) -> Dict[str, Any]:
    r = _new_sql_result(q, db, agent)
    if stopped:
        r["source"] = "not attempted"
        r["verdict"] = "NOT EVALUATED"
        r["failures"].append(f"Not run: evaluation stopped after a gateway/credit/quota/auth error ({stopped[:120]}).")
        return r

    try:
        out = generator.generate_structured(q["question"])  # exactly one adapter call
    except GeminiGenerationError as exc:
        r["latency"], r["raw"] = adapter.last_latency, adapter.last_raw
        if adapter.gateway_empty:
            r["generation"] = "failed (gateway returned no content)"
            r["verdict"] = "NOT EVALUATED"
            r["failures"].append(f"Gateway error, model output not available: {exc}")
            r["stop"] = str(exc)
            return r
        api_side = exc.category in {"timeout", "quota", "auth", "network", "bad_request", "server",
                                    "api_error", "model_not_found"}
        if api_side:
            r["generation"] = f"failed ({exc.category})"
        else:
            r["generation"] = "success"
            r["parse"] = f"rejected ({exc.category})"
            if exc.category == "unsafe_sql":
                r["safety"] = "REJECTED (not executed)"
        r["verdict"] = "FAIL"
        r["failures"].append(f"{exc.category}: {exc}")
        if exc.category in STOP_CATEGORIES:
            r["stop"] = str(exc)
        return r

    r["latency"], r["raw"] = adapter.last_latency, adapter.last_raw
    r["generation"] = "success"
    r["parse"] = "accepted"
    r["sql"], r["params"], r["description"] = out["sql"], list(out["parameters"]), out["description"]
    # parse_gemini_response already ran validate_read_only_sql; re-check explicitly anyway.
    return _validate_execute_compare(r, db)


# ───────────────────────────────────────────────────────────── Q9-Q10


def run_exposure_question(q, db, det_agent) -> Dict[str, Any]:
    qr = evaluate_question(det_agent, q, db.db_path, DATA_DIR)  # existing evaluator logic
    response = det_agent.answer_question(q["question"])  # deterministic; for counts only
    result = response.get("result") or {}
    expected = compute_expected_exposure(DATA_DIR, q["ground_truth"]["parameters"]["portfolio_name"])
    checks = {
        "equity_holdings_count": (result.get("equity_holdings_count"), expected["equity_holdings"]),
        "excluded_bond_holdings": (result.get("excluded_bond_holdings"), expected["excluded_bond_holdings"]),
        "total_equity_weight_pct": (result.get("total_equity_weight_pct"),
                                    round(sum(expected["exposures_pct"].values()), 6)),
    }
    count_fail = [
        f"{k}: expected {e}, got {a}" for k, (a, e) in checks.items()
        if a is None or abs(float(a) - float(e)) > EXPOSURE_TOL_PP
    ]
    passed = qr.passed and not count_fail
    return {
        "id": qr.qid, "type": qr.qtype, "difficulty": qr.difficulty, "question": qr.question,
        "expected_tool": qr.expected_tool, "routed_tool": qr.selected_tool,
        "generation": "n/a (deterministic, no Argon request)", "latency": qr.duration_ms / 1000.0,
        "parse": "n/a", "safety": "n/a", "execution": qr.agent_status or "",
        "sql": "", "params": [], "description": "", "raw": "", "official_sql": "", "ordered": False,
        "exp_cols": qr.expected_columns, "exp_rows": qr.expected_rows,
        "act_cols": qr.actual_columns, "act_rows": qr.actual_rows,
        "verdict": "PASS" if passed else "FAIL",
        "failures": list(qr.failures) + count_fail, "notes": list(qr.notes),
        "portfolio": result.get("portfolio_name"), "checks": checks,
        "total_holdings_count": result.get("total_holdings_count"),
    }


def bond_exclusion_check(db) -> Dict[str, Any]:
    """Supplementary: run the ExposureCalculator on a real mixed stock/bond portfolio."""
    calc = ExposureCalculator(db=db)
    res = calc.calculate(BOND_CHECK_PORTFOLIO)
    expected = compute_expected_exposure(DATA_DIR, BOND_CHECK_PORTFOLIO)
    actual = {e["sector"]: e["exposure_pct"] for e in res.get("exposures", [])}
    ok = (
        set(actual) == set(expected["exposures_pct"])
        and all(abs(actual[s] - v) <= EXPOSURE_TOL_PP for s, v in expected["exposures_pct"].items())
        and res.get("excluded_bond_holdings") == expected["excluded_bond_holdings"]
    )
    return {"portfolio": BOND_CHECK_PORTFOLIO, "ok": ok, "actual": res, "expected": expected}


# ───────────────────────────────────────────────────────────── outputs

CSV_FIELDS = [
    "qid", "type", "difficulty", "question", "expected_tool", "routed_tool", "generation", "latency_s",
    "parse_validation", "safety_validation", "execution", "expected_row_count", "actual_row_count",
    "ordered_comparison", "verdict", "failure_reasons", "notes", "generated_sql", "parameters",
    "model_description", "previous_verdict", "previous_latency_s", "source",
]


def load_previous_csv() -> Dict[int, Dict[str, str]]:
    if not CSV_PATH.is_file():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as fh:
        return {int(row["qid"]): row for row in csv.DictReader(fh) if row.get("qid")}


def write_csv(results: List[Dict[str, Any]]) -> None:
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in results:
            prev = PREVIOUS.get(r["id"])
            w.writerow({
                "qid": r["id"], "type": r["type"], "difficulty": r["difficulty"], "question": r["question"],
                "expected_tool": r["expected_tool"], "routed_tool": r["routed_tool"],
                "generation": r["generation"], "latency_s": f"{r['latency']:.3f}" if r["latency"] is not None else "",
                "parse_validation": r["parse"], "safety_validation": r["safety"], "execution": r["execution"],
                "expected_row_count": len(r["exp_rows"]), "actual_row_count": len(r["act_rows"]),
                "ordered_comparison": r["ordered"], "verdict": r["verdict"],
                "failure_reasons": " | ".join(r["failures"]), "notes": " | ".join(r["notes"]),
                "generated_sql": r["sql"], "parameters": json.dumps(r["params"]),
                "model_description": r["description"],
                "previous_verdict": prev[0] if prev else "n/a (not sent to Argon)",
                "previous_latency_s": prev[1] if prev else "",
                "source": r.get("source", "deterministic"),
            })


def render(results, bond, meta) -> str:
    sql_r = [r for r in results if r["type"] == "text2sql"]
    exp_r = [r for r in results if r["type"] != "text2sql"]
    total = sum(r["verdict"] == "PASS" for r in results)
    sql_pass = sum(r["verdict"] == "PASS" for r in sql_r)
    not_eval = [r for r in sql_r if r["verdict"] == "NOT EVALUATED"]
    # Latency only for real model answers (excludes empty gateway replies).
    lat = [r["latency"] for r in sql_r if r["latency"] is not None and r["verdict"] in ("PASS", "FAIL")]
    carried = [r["id"] for r in sql_r if r.get("source") == "carried over"]
    L: List[str] = []
    L += ["# PL Capital — Full Q1–Q10 Assessment with gemini-4-argon\n",
          f"Run: {meta['run_at']} · Q1–Q8: `{MODEL_ID}` via the 1min.AI gateway (`{ENDPOINT}`) · "
          f"sent to Argon in this run: {', '.join(f'Q{i}' for i in meta['selected'])} "
          f"({meta['requests']} question request(s)"
          + (", plus 1 preflight probe" if meta.get("preflight") else "") + ")"
          + (f" · carried over from the previous recorded run (SQL re-executed locally, no request): "
             f"{', '.join(f'Q{i}' for i in carried)}" if carried else "")
          + " · Q9–Q10: deterministic `ExposureCalculator` (no LLM). "
          "Generated entirely by `scripts/test_argon_full_assessment.py`. No API key appears in this report.\n"]
    if meta.get("preflight"):
        pf = meta["preflight"]
        L.append(f"Preflight probe: {'OK' if pf['ok'] else 'FAILED'} ({pf['latency']:.2f}s) — {pf['detail']}\n")
    if meta.get("stopped"):
        L.append(f"> **Run stopped early** (gateway / credit / quota / auth): {meta['stopped'][:300]}\n")

    L += ["## Overall score\n",
          f"| Scope | Score |\n|---|---:|\n| **Q1–Q10 overall** | **{total}/10** |\n"
          f"| Q1–Q8 text-to-SQL (Argon, production prompt) | {sql_pass}/8 |\n"
          f"| Q1–Q8 not evaluated | {len(not_eval)} |\n"
          f"| Q9–Q10 exposure (deterministic) | {sum(r['verdict'] == 'PASS' for r in exp_r)}/2 |\n",
          ("Argon latency (answered requests only): " + f"min {min(lat):.2f}s · median {sorted(lat)[len(lat) // 2]:.2f}s · "
           f"max {max(lat):.2f}s\n") if lat else ""]

    L += ["## Scorecard\n",
          "| Q | Source | Tool (expected → routed) | Generation | Parse | Safety | Execution | Rows exp/act | Latency | Ground truth |",
          "|---|---|---|---|---|---|---|---|---:|---|"]
    for r in results:
        if r["type"] == "text2sql":
            act = len(r["act_rows"]) if r["execution"] == "success" else "—"
            L.append(f"| Q{r['id']} | {r.get('source', '')} | {r['expected_tool']} → {r['routed_tool']} | "
                     f"{r['generation']} | {r['parse']} | {r['safety'].split(':')[0]} | "
                     f"{r['execution'].split(':')[0]} | {len(r['exp_rows'])}/{act} | "
                     f"{secs(r['latency'])} | **{r['verdict']}** |")
        else:
            L.append(f"| Q{r['id']} | deterministic | {r['expected_tool']} → {r['routed_tool']} | n/a | n/a | n/a | "
                     f"{r['execution']} | {len(r['exp_rows'])}/{len(r['act_rows'])} sectors | "
                     f"{secs(r['latency'])} | **{r['verdict']}** |")
    L.append("")

    L += ["## Method\n",
          "- **Prompt (Q1–Q8):** the production `GeminiSQLGenerator.build_system_instruction()` — live schema "
          "introspected from `database/portfolio.db`, the production `FINANCIAL_SEMANTICS` rules, categorical value "
          "hints and the strict output rules — plus the production `RESPONSE_JSON_SCHEMA` and user prompt "
          "`Question: …`. No expected answers or official SQL are in the prompt.",
          "- **Gateway difference:** the Google API takes `system_instruction` and a JSON response schema natively; "
          "1min.AI takes one prompt string, so both are sent as text. JSON output is therefore requested, not "
          "enforced by the gateway; the production parser still enforces it.",
          "- **Validation:** production `parse_gemini_response()` (JSON shape, parameter types, positional "
          "placeholders, placeholder count, `validate_read_only_sql()`), then an explicit `validate_read_only_sql()` "
          "re-check, then `PortfolioDatabase.execute_read_only(sql, params)` (`mode=ro`, `query_only`, authorizer).",
          f"- **Scoring:** `evaluate.compare_tables()` against the official SQL run by `evaluate.run_expected_sql()` "
          f"— abs {ABS_TOL:g} / rel {REL_TOL:g}; all expected columns required (names may differ, extras ignored); "
          "ordered when the official SQL has `ORDER BY`. Routing is checked with the production agent's router.",
          "- **Q9–Q10:** `evaluate.evaluate_question()` with `PortfolioAgent` (Gemini disabled) → `ExposureCalculator`; "
          f"expected values computed independently from the CSVs (tolerance {EXPOSURE_TOL_PP:g} pp). The ground-truth "
          "file defines the target portfolio and rules, not numeric values.",
          "- One request per question, sequential, no retries; an empty gateway reply or a credit / quota / auth "
          "error stops the run and later questions are marked NOT EVALUATED.",
          "- Carried-over questions reuse the SQL recorded by the previous run's CSV; they are re-validated and "
          "re-executed locally against the current database and re-scored with the same evaluator rules.\n"]

    L += ["## Comparison with the previous Argon run\n",
          "Previous run: schema-only prompt, plain-SQL output (Q1 via `test_argon_portfolio.py`, Q2–Q8 via "
          "`test_argon_q2_q8.py`). This run: production prompt with financial semantics, value hints and JSON output.\n",
          "| Q | Previous | Previous issue | This run | Latency prev → now |", "|---|---|---|---|---|"]
    for r in sql_r:
        p = PREVIOUS.get(r["id"], ("—", None, ""))
        L.append(f"| Q{r['id']} | {p[0]} | {p[2] or '—'} | **{r['verdict']}** | {secs(p[1])} → {secs(r['latency'])} |")
    prev_total = sum(v[0] == "PASS" for v in PREVIOUS.values())
    L.append(f"| **Total** | **{prev_total}/8** | | **{sql_pass}/8**"
             + (f" ({len(not_eval)} not evaluated)" if not_eval else "") + " | |\n")

    L.append("## Q1–Q8 details\n")
    for r in sql_r:
        L.append(f"### Q{r['id']} — {r['question']}\n")
        L.append(f"**{r['verdict']}** · generation {r['generation']} · parse {r['parse']} · safety {r['safety']} · "
                 f"execution {r['execution']} · latency {secs(r['latency'])}\n")
        if r["sql"]:
            L.append("Argon SQL" + (f" · parameters `{json.dumps(r['params'])}`" if r["params"] else "") + ":\n")
            L.append("```sql\n" + r["sql"] + "\n```\n")
            if r["description"]:
                L.append(f"Model description: _{r['description']}_\n")
        elif r["raw"]:
            L.append("Raw response (not accepted):\n\n```\n" + r["raw"][:1500] + "\n```\n")
        L.append("<details><summary>Official SQL</summary>\n\n```sql\n" + r["official_sql"] + "\n```\n</details>\n")
        L.append("Expected (official SQL):\n\n" + md_table(r["exp_cols"], r["exp_rows"]) + "\n")
        if r["execution"] == "success":
            L.append("Actual (Argon SQL):\n\n" + md_table(r["act_cols"], r["act_rows"]) + "\n")
        L += [f"- ❌ {f}" for f in r["failures"]] + [f"- ℹ️ {n}" for n in r["notes"]] + [""]

    L.append("## Q9–Q10 details (deterministic ExposureCalculator)\n")
    for r in exp_r:
        L.append(f"### Q{r['id']} — {r['question']}\n")
        L.append(f"**{r['verdict']}** · resolved portfolio **{r['portfolio']}** · routed to `{r['routed_tool']}` · "
                 f"no Argon request\n")
        L.append("Expected (computed from CSVs):\n\n" + md_table(["sector", "exposure_pct"], r["exp_rows"]) + "\n")
        L.append("Actual (ExposureCalculator):\n\n" + md_table(["sector", "exposure_pct"], r["act_rows"]) + "\n")
        L.append("| Check | Expected | Actual |\n|---|---:|---:|")
        for k, (a, e) in r["checks"].items():
            L.append(f"| {k} | {fmt(e)} | {fmt(a)} |")
        L.append(f"| total_holdings_count | — | {fmt(r['total_holdings_count'])} |\n")
        L += [f"- ❌ {f}" for f in r["failures"]] + [f"- ℹ️ {n}" for n in r["notes"]] + [""]

    a, e = bond["actual"], bond["expected"]
    L += ["### Supplementary real-data bond-exclusion check (not an official question)\n",
          f"Q9 and Q10 portfolios hold no bonds, so bond exclusion is not exercised by them. The same calculator on "
          f"**{bond['portfolio']}**: {e['equity_holdings']} equity + {e['excluded_bond_holdings']} bond holdings "
          f"(CSV) → calculator reported {a.get('equity_holdings_count')} equity, "
          f"{a.get('excluded_bond_holdings')} bonds excluded, total equity weight "
          f"{fmt(a.get('total_equity_weight_pct'))}% (not renormalised to 100%). "
          f"Sector values match the CSV computation: **{'yes' if bond['ok'] else 'NO'}**.\n",
          md_table(["sector", "exposure_pct (calculator)", "expected (CSV)"],
                   [(x["sector"], x["exposure_pct"], e["exposures_pct"].get(x["sector"])) for x in a.get("exposures", [])]),
          ""]
    return "\n".join(L)


# ───────────────────────────────────────────────────────────── main


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Q1-Q10 assessment with gemini-4-argon (Q1-Q8) via 1min.AI.")
    p.add_argument("--questions", help="Comma-separated SQL question IDs (1-8) to send to Argon. "
                                       "Default: all. Others are carried over from the previous CSV.")
    p.add_argument("--preflight", action="store_true",
                   help="Send one minimal probe request first; abort if no usable text is returned.")
    args = p.parse_args(argv)
    if args.questions:
        try:
            ids = {int(x) for x in args.questions.split(",") if x.strip()}
        except ValueError:
            p.error("--questions must be comma-separated integers")
        if not ids or not ids <= set(range(1, 9)):
            p.error("--questions accepts SQL question IDs 1-8 only (Q9-Q10 are deterministic)")
        args.selected = ids
    else:
        args.selected = set(range(1, 9))
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    api_key = load_onemin_key()
    print(f"ONEMIN_API_KEY: {'present' if api_key else 'MISSING'} | model {MODEL_ID} | {ENDPOINT}")
    print(f"SQL questions sent to Argon: {sorted(args.selected)}")
    if not api_key:
        return 2
    db = PortfolioDatabase()
    if not db.is_initialized():
        print("Database not initialised. Run: python init_db.py")
        return 1

    adapter = OneMinGeminiAdapter(api_key)
    preflight = None
    if args.preflight:
        ok, latency, detail = adapter.probe()
        preflight = {"ok": ok, "latency": latency, "detail": detail}
        print(f"Preflight probe: {'OK' if ok else 'FAILED'} ({latency:.2f}s) {detail}")
        if not ok:
            print("Gateway is not returning usable output. No questions sent; result files left unchanged.")
            db.close()
            return 3
    previous_csv = load_previous_csv()
    # Production generator, unmodified; api_key=None so GEMINI_API_KEY is never used.
    generator = GeminiSQLGenerator(api_key="", model=MODEL_ID, db=db, client=adapter)
    router = PortfolioAgent(db=db, sql_tool=SQLQueryTool(db=db, use_gemini=False))
    det_agent = PortfolioAgent(db=db, sql_tool=SQLQueryTool(db=db, use_gemini=False))

    questions = load_ground_truth(GROUND_TRUTH)
    results: List[Dict[str, Any]] = []
    stopped: Optional[str] = None
    for q in questions:
        if q["type"] == "text2sql" and int(q["id"]) not in args.selected:
            print(f"Q{q['id']} (carried over, no request) ... ", end="", flush=True)
            r = carry_over_sql_question(q, db, router, previous_csv.get(int(q["id"])))
            print(r["verdict"])
        elif q["type"] == "text2sql":
            print(f"Q{q['id']} (Argon) ... ", end="", flush=True)
            r = run_sql_question(q, db, generator, adapter, router, stopped)
            if r.get("stop"):
                stopped = r["stop"]
            print(f"{secs(r['latency'])} {r['verdict']}" + (f"  [{r['failures'][0][:90]}]" if r["failures"] else ""))
        else:
            print(f"Q{q['id']} (deterministic) ... ", end="", flush=True)
            r = run_exposure_question(q, db, det_agent)
            print(r["verdict"])
        results.append(r)
    bond = bond_exclusion_check(db)
    db.close()

    total = sum(r["verdict"] == "PASS" for r in results)
    meta = {"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "requests": adapter.calls,
            "stopped": stopped, "selected": sorted(args.selected), "preflight": preflight}
    write_csv(results)
    MD_PATH.write_text(render(results, bond, meta), encoding="utf-8")
    print(f"\nArgon question requests sent: {adapter.calls}"
          + (f" (+{getattr(adapter, 'probe_calls', 0)} preflight probe)" if preflight else ""))
    print(f"Supplementary bond-exclusion check ({BOND_CHECK_PORTFOLIO}): {'OK' if bond['ok'] else 'FAILED'}")
    print(f"Overall: {total}/10  ->  {MD_PATH.name}, {CSV_PATH.name}")
    return 0 if total == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())

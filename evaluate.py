#!/usr/bin/env python3
"""
PL Capital AI Portfolio Analytics Agent - Ground-Truth Evaluator.

Runs every question in ground_truth_dataset.json through the real PortfolioAgent and
compares the agent's answer with an independently computed expected answer:

- text2sql questions (Q1-Q8): the OFFICIAL ground-truth SQL is executed directly against
  the SQLite database on a separate read-only sqlite3 connection (not through the agent,
  its SQL generators or its database layer).
- exposure_calculator questions (Q9-Q10): equity sector exposure is recomputed from the
  ORIGINAL CSV files with the csv module: SUM(current_weight) of Stock holdings grouped by
  sector, x100, bonds excluded, no renormalisation.

Usage:
    python evaluate.py                 # deterministic (rule-based SQL), no API key needed
    python evaluate.py --use-llm       # Gemini-enabled SQL generation (live API calls)
    python evaluate.py --report path/to/report.md --db path/to/portfolio.db

Exit code: 0 when every question passes, 1 otherwise, 2 on setup errors.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.agent import PortfolioAgent  # noqa: E402
from portfolio_agent.database import PortfolioDatabase  # noqa: E402
from portfolio_agent.tools import SQLQueryTool  # noqa: E402

DEFAULT_GROUND_TRUTH = PROJECT_ROOT / "ground_truth_dataset.json"
DEFAULT_REPORT = PROJECT_ROOT / "evaluation_report.md"

# Numeric tolerance for SQL result comparison (values are produced by the same SQLite
# engine, so differences should only ever be floating-point noise).
ABS_TOL = 1e-6
REL_TOL = 1e-9
# Tolerance for exposure percentages, in percentage points.
EXPOSURE_TOL_PP = 1e-4

EXPECTED_TOOL_BY_TYPE = {"text2sql": "sql_query", "exposure_calculator": "exposure_calculator"}

Row = Tuple[Any, ...]


# ─────────────────────────────────────────────────────────────── data classes


@dataclass
class QuestionResult:
    qid: int
    qtype: str
    difficulty: str
    question: str
    passed: bool = False
    expected_tool: str = ""
    selected_tool: Optional[str] = None
    agent_status: Optional[str] = None
    generation_method: Optional[str] = None
    model_name: Optional[str] = None
    fallback_used: bool = False
    fallback_reason: Optional[str] = None
    generated_sql: Optional[str] = None
    expected_columns: List[str] = field(default_factory=list)
    expected_rows: List[Row] = field(default_factory=list)
    actual_columns: List[str] = field(default_factory=list)
    actual_rows: List[Row] = field(default_factory=list)
    ordered_comparison: bool = False
    failures: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    duration_ms: float = 0.0


GEMINI_CLIENT_STATES = ("none", "live", "mock", "unconfigured")


@dataclass
class EvaluationRun:
    mode: str  # "deterministic" | "llm"
    gemini_client: str  # one of GEMINI_CLIENT_STATES
    model_name: Optional[str]
    results: List[QuestionResult]
    started_at: str
    duration_s: float
    db_path: str
    data_dir: str
    integrity: Dict[str, Any]
    # Set when only a subset of the ground-truth questions was evaluated (--questions).
    question_subset: Optional[List[int]] = None
    ground_truth_total: Optional[int] = None

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)


# ─────────────────────────────────────────────────────────────── comparison


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def values_equal(expected: Any, actual: Any) -> bool:
    """Compare two scalar values with numeric tolerance; strings and NULLs compare exactly."""
    if _is_number(expected) and _is_number(actual):
        if math.isnan(float(expected)) or math.isnan(float(actual)):
            return False
        return math.isclose(float(expected), float(actual), rel_tol=REL_TOL, abs_tol=ABS_TOL)
    return expected == actual


def rows_equal(expected: Sequence[Any], actual: Sequence[Any]) -> bool:
    return len(expected) == len(actual) and all(values_equal(e, a) for e, a in zip(expected, actual))


def _sort_key(row: Sequence[Any]) -> Tuple:
    """Canonical, type-safe sort key (numbers rounded so tolerance-equal values sort together)."""
    key = []
    for v in row:
        if v is None:
            key.append((0, ""))
        elif _is_number(v):
            key.append((1, round(float(v), 6)))
        else:
            key.append((2, str(v)))
    return tuple(key)


def _column(rows: Sequence[Row], idx: int) -> List[Any]:
    return [r[idx] for r in rows]


def _map_columns(expected_rows: Sequence[Row], actual_rows: Sequence[Row], n_expected: int, n_actual: int) -> Optional[List[int]]:
    """
    Map each expected column to an actual column whose values match as a multiset.
    Allows the agent to use different aliases, different column order or extra columns
    (e.g. Gemini returning an additional id column). Returns None if no full mapping exists.
    """
    mapping: List[int] = []
    used: set = set()
    for j in range(n_expected):
        exp_sorted = sorted(_column(expected_rows, j), key=lambda v: _sort_key((v,)))
        found = None
        for k in range(n_actual):
            if k in used:
                continue
            act_sorted = sorted(_column(actual_rows, k), key=lambda v: _sort_key((v,)))
            if rows_equal(exp_sorted, act_sorted):
                found = k
                break
        if found is None:
            return None
        mapping.append(found)
        used.add(found)
    return mapping


def compare_tables(
    expected_columns: Sequence[str],
    expected_rows: Sequence[Row],
    actual_columns: Sequence[str],
    actual_rows: Sequence[Row],
    ordered: bool,
) -> Tuple[bool, List[str], List[str]]:
    """
    Compare a query result against the expected result.

    Returns (passed, failure_reasons, notes). Column names are not required to match
    (aliases such as COUNT(*) vs total are cosmetic); values, row count and - when the
    official SQL has ORDER BY - row order must match.
    """
    failures: List[str] = []
    notes: List[str] = []

    if len(actual_rows) != len(expected_rows):
        failures.append(f"Row count mismatch: expected {len(expected_rows)}, got {len(actual_rows)}.")
        return False, failures, notes

    n_exp, n_act = len(expected_columns), len(actual_columns)
    if n_act < n_exp:
        failures.append(f"Column count mismatch: expected {n_exp} columns, got {n_act}.")
        return False, failures, notes

    if not expected_rows:
        return True, failures, ["Both results are empty."]

    mapping: Optional[List[int]] = list(range(n_exp)) if n_act == n_exp else None
    if mapping is not None:
        projected = [tuple(r[i] for i in mapping) for r in actual_rows]
        positional_ok = (
            all(rows_equal(e, a) for e, a in zip(expected_rows, projected))
            if ordered
            else all(
                rows_equal(e, a)
                for e, a in zip(sorted(expected_rows, key=_sort_key), sorted(projected, key=_sort_key))
            )
        )
        if not positional_ok:
            mapping = None

    if mapping is None:
        mapping = _map_columns(expected_rows, actual_rows, n_exp, n_act)
        if mapping is None:
            failures.append("Result values do not match the expected values (no column of the answer matches "
                            "one or more expected columns).")
            return False, failures, notes
        if mapping != list(range(n_exp)) or n_act != n_exp:
            notes.append(
                "Columns matched by value: "
                + ", ".join(f"{expected_columns[j]} -> {actual_columns[k]}" for j, k in enumerate(mapping))
                + (f" ({n_act - n_exp} extra column(s) ignored)." if n_act > n_exp else ".")
            )

    projected = [tuple(r[i] for i in mapping) for r in actual_rows]
    exp_sorted = sorted(expected_rows, key=_sort_key)
    act_sorted = sorted(projected, key=_sort_key)
    unordered_ok = all(rows_equal(e, a) for e, a in zip(exp_sorted, act_sorted))

    if not unordered_ok:
        failures.append("Result rows do not match the expected rows.")
        return False, failures, notes

    if ordered:
        if not all(rows_equal(e, a) for e, a in zip(expected_rows, projected)):
            failures.append("Rows contain the expected values but are not in the order required by the official "
                            "query's ORDER BY.")
            return False, failures, notes
        notes.append("Row order verified (official query uses ORDER BY).")

    if [c.lower() for c in actual_columns[:n_exp]] != [c.lower() for c in expected_columns]:
        notes.append("Column names differ from the official query (cosmetic; values match).")
    return True, failures, notes


# ─────────────────────────────────────────────────────────────── expected results


def load_ground_truth(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    questions = data.get("questions")
    if not isinstance(questions, list) or not questions:
        raise ValueError(f"No questions found in {path}")
    return questions


def _connect_read_only(db_path: Path) -> sqlite3.Connection:
    """Independent read-only connection (deliberately not PortfolioDatabase)."""
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON;")
    return conn


def run_expected_sql(db_path: Path, sql: str) -> Tuple[List[str], List[Row]]:
    """Execute the official ground-truth SQL exactly as supplied."""
    conn = _connect_read_only(db_path)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchall()]
        return columns, rows
    finally:
        conn.close()


def _read_csv(data_dir: Path, name: str) -> List[Dict[str, str]]:
    with open(data_dir / f"{name}.csv", "r", encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def compute_expected_exposure(data_dir: Path, portfolio_name: str) -> Dict[str, Any]:
    """
    Independently compute equity sector exposure from the original CSV files.

    exposure_pct(sector) = SUM(holdings.current_weight) * 100 over Stock holdings in that
    sector. Bonds (asset_type='Bond', NULL sector) are excluded. No renormalisation.
    """
    portfolios = _read_csv(data_dir, "portfolios")
    matches = [p for p in portfolios if p["portfolio_name"] == portfolio_name]
    if len(matches) != 1:
        raise ValueError(f"Portfolio '{portfolio_name}' not found exactly once in portfolios.csv")
    portfolio_id = matches[0]["portfolio_id"]

    securities = {s["security_id"]: s for s in _read_csv(data_dir, "securities")}
    sectors = {s["sector_id"]: s["sector_name"] for s in _read_csv(data_dir, "sectors")}

    weights: Dict[str, float] = {}
    bonds = stocks = 0
    for h in _read_csv(data_dir, "holdings"):
        if h["portfolio_id"] != portfolio_id:
            continue
        sec = securities[h["security_id"]]
        if sec["asset_type"] != "Stock":
            bonds += 1
            continue
        stocks += 1
        if not sec["sector_id"]:
            continue
        sector = sectors[sec["sector_id"]]
        weights[sector] = weights.get(sector, 0.0) + float(h["current_weight"])

    return {
        "portfolio_id": int(portfolio_id),
        "portfolio_name": portfolio_name,
        "exposures_pct": {k: round(v * 100.0, 6) for k, v in weights.items()},
        "equity_holdings": stocks,
        "excluded_bond_holdings": bonds,
    }


def check_integrity(db_path: Path, data_dir: Path) -> Dict[str, Any]:
    """Compare per-table row counts in SQLite with the original CSV row counts."""
    conn = _connect_read_only(db_path)
    tables: Dict[str, Dict[str, int]] = {}
    try:
        for csv_path in sorted(data_dir.glob("*.csv")):
            name = csv_path.stem
            with open(csv_path, "r", encoding="utf-8-sig", newline="") as fh:
                csv_rows = sum(1 for _ in csv.DictReader(fh))
            db_rows = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            tables[name] = {"csv": csv_rows, "db": db_rows}
        fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    finally:
        conn.close()
    return {
        "tables": tables,
        "csv_total": sum(t["csv"] for t in tables.values()),
        "db_total": sum(t["db"] for t in tables.values()),
        "matches": all(t["csv"] == t["db"] for t in tables.values()),
        "fk_violations": fk_violations,
    }


# ─────────────────────────────────────────────────────────────── evaluation


def _evaluate_sql_question(q: Dict[str, Any], res: QuestionResult, response: Dict[str, Any], db_path: Path) -> None:
    gt = q["ground_truth"]
    sql = gt["sql_query"]
    res.ordered_comparison = "ORDER BY" in sql.upper()
    res.expected_columns, res.expected_rows = run_expected_sql(db_path, sql)

    result = response.get("result") or {}
    res.generation_method = result.get("generation_method")
    res.model_name = result.get("model_name")
    res.fallback_used = bool(result.get("fallback_used"))
    res.fallback_reason = result.get("fallback_reason")
    res.generated_sql = result.get("generated_sql")
    res.actual_columns = list(result.get("columns") or [])
    res.actual_rows = [tuple(row.get(c) for c in res.actual_columns) for row in (result.get("rows") or [])]

    if response.get("status") != "success":
        res.failures.append(f"Agent returned status '{response.get('status')}': {response.get('error')}")
        return

    ok, failures, notes = compare_tables(
        res.expected_columns, res.expected_rows, res.actual_columns, res.actual_rows, res.ordered_comparison
    )
    res.failures.extend(failures)
    res.notes.extend(notes)


def _evaluate_exposure_question(q: Dict[str, Any], res: QuestionResult, response: Dict[str, Any], data_dir: Path) -> None:
    gt = q["ground_truth"]
    expected_name = gt["parameters"]["portfolio_name"]
    expected = compute_expected_exposure(data_dir, expected_name)
    res.generation_method = "deterministic"
    res.expected_columns = ["sector", "exposure_pct"]
    res.expected_rows = sorted(expected["exposures_pct"].items(), key=lambda kv: (-kv[1], kv[0]))

    result = response.get("result") or {}
    exposures = result.get("exposures") or []
    actual = {e.get("sector"): e.get("exposure_pct") for e in exposures}
    res.actual_columns = ["sector", "exposure_pct"]
    res.actual_rows = [(e.get("sector"), e.get("exposure_pct")) for e in exposures]

    if response.get("status") != "success":
        res.failures.append(f"Agent returned status '{response.get('status')}': {response.get('error')}")
        return

    if result.get("portfolio_name") != expected_name:
        res.failures.append(
            f"Resolved portfolio '{result.get('portfolio_name')}' but ground truth expects '{expected_name}'."
        )

    exp_map = expected["exposures_pct"]
    missing = sorted(set(exp_map) - set(actual))
    extra = sorted(set(actual) - set(exp_map))
    if missing:
        res.failures.append(f"Missing sectors: {missing}.")
    if extra:
        res.failures.append(f"Unexpected sectors (e.g. bonds or unmapped holdings not excluded): {extra}.")
    for sector in sorted(set(exp_map) & set(actual)):
        a = actual[sector]
        if not _is_number(a) or abs(float(a) - exp_map[sector]) > EXPOSURE_TOL_PP:
            res.failures.append(f"{sector}: expected {exp_map[sector]:.4f}%, got {a}%.")

    if result.get("excluded_bond_holdings") != expected["excluded_bond_holdings"]:
        res.failures.append(
            f"Excluded bond holdings: expected {expected['excluded_bond_holdings']}, "
            f"got {result.get('excluded_bond_holdings')}."
        )

    total = sum(exp_map.values())
    res.notes.append(
        f"Expected computed from original CSVs: {len(exp_map)} sector(s), {expected['equity_holdings']} equity "
        f"holding(s), {expected['excluded_bond_holdings']} bond holding(s) excluded, total equity weight "
        f"{total:.2f}% (not renormalised)."
    )


def evaluate_question(agent: PortfolioAgent, q: Dict[str, Any], db_path: Path, data_dir: Path) -> QuestionResult:
    res = QuestionResult(
        qid=int(q["id"]),
        qtype=q["type"],
        difficulty=q.get("difficulty", ""),
        question=q["question"],
        expected_tool=EXPECTED_TOOL_BY_TYPE.get(q["type"], q["type"]),
    )
    started = time.perf_counter()
    try:
        response = agent.answer_question(q["question"])
    except Exception as exc:  # the evaluator must never crash on an agent bug
        res.failures.append(f"Agent raised {type(exc).__name__}: {exc}")
        res.duration_ms = (time.perf_counter() - started) * 1000
        return res
    res.duration_ms = (time.perf_counter() - started) * 1000
    res.selected_tool = response.get("selected_tool")
    res.agent_status = response.get("status")

    if res.selected_tool != res.expected_tool:
        res.failures.append(f"Routed to '{res.selected_tool}' but ground truth expects '{res.expected_tool}'.")

    try:
        if q["type"] == "text2sql":
            _evaluate_sql_question(q, res, response, db_path)
        elif q["type"] == "exposure_calculator":
            _evaluate_exposure_question(q, res, response, data_dir)
        else:
            res.failures.append(f"Unknown question type '{q['type']}'.")
    except Exception as exc:
        res.failures.append(f"Evaluator error while checking this question: {type(exc).__name__}: {exc}")

    res.passed = not res.failures
    return res


def run_evaluation(
    agent: PortfolioAgent,
    questions: List[Dict[str, Any]],
    db_path: Path,
    data_dir: Path,
    mode: str,
    gemini_client: str = "none",
    model_name: Optional[str] = None,
) -> EvaluationRun:
    """
    ``gemini_client`` describes the Gemini client actually used, so the report can never
    present mocked or unconfigured runs as live evidence:
    none (deterministic), live (real API), mock (injected test client), unconfigured (no key).
    """
    if gemini_client not in GEMINI_CLIENT_STATES:
        raise ValueError(f"gemini_client must be one of {GEMINI_CLIENT_STATES}")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    t0 = time.perf_counter()
    integrity = check_integrity(db_path, data_dir)
    results = [evaluate_question(agent, q, db_path, data_dir) for q in questions]
    return EvaluationRun(
        mode=mode,
        gemini_client=gemini_client,
        model_name=model_name,
        results=results,
        started_at=started_at,
        duration_s=time.perf_counter() - t0,
        db_path=str(db_path),
        data_dir=str(data_dir),
        integrity=integrity,
    )


# ─────────────────────────────────────────────────────────────── reporting


def _fmt(value: Any) -> str:
    if _is_number(value) and isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return "NULL" if value is None else str(value)


def _md_table(columns: Sequence[str], rows: Sequence[Row], limit: int = 15) -> str:
    if not columns:
        return "_(no columns)_"
    esc = lambda s: str(s).replace("|", "\\|")  # noqa: E731
    lines = ["| " + " | ".join(esc(c) for c in columns) + " |", "|" + "---|" * len(columns)]
    for r in rows[:limit]:
        lines.append("| " + " | ".join(esc(_fmt(v)) for v in r) + " |")
    if not rows:
        lines.append("| " + " | ".join("" for _ in columns) + " |")
    if len(rows) > limit:
        lines.append(f"\n_... {len(rows) - limit} more row(s) not shown._")
    return "\n".join(lines)


def verification_status(run: EvaluationRun) -> List[str]:
    sql_results = [r for r in run.results if r.qtype == "text2sql"]
    gemini_answers = sum(1 for r in sql_results if r.generation_method == "gemini")
    lines = []
    if run.mode == "deterministic":
        lines.append("Gemini was **disabled** for this run. All SQL was produced by the deterministic "
                     "rule-based generator. **This run says nothing about live Gemini performance.**")
    elif run.gemini_client == "unconfigured":
        lines.append("Gemini was enabled but **not configured** (no GEMINI_API_KEY). No API requests were made; "
                     "every SQL question used the rule-based fallback. **Live Gemini performance was not evaluated.**")
    elif run.gemini_client == "mock":
        lines.append("Gemini was enabled with an **injected/mocked client**. These results are **not** evidence "
                     "of live Gemini API performance.")
    else:
        lines.append(f"Gemini was enabled and live API requests were attempted with model `{run.model_name}`. "
                     f"{gemini_answers} of {len(sql_results)} SQL questions were answered with Gemini-generated SQL; "
                     f"{len(sql_results) - gemini_answers} used the rule-based fallback (reasons listed per question).")
    lines.append("Exposure questions (Q9-Q10) are always computed deterministically by the ExposureCalculator; "
                 "Gemini is never used for financial calculations.")
    return lines


def render_report(run: EvaluationRun) -> str:
    mode_label = {
        "deterministic": "Deterministic (rule-based SQL generation, no API key required)",
        "llm": "Gemini-enabled SQL generation" + {
            "live": " (live API)",
            "mock": " (injected/mocked client)",
            "unconfigured": " (not configured - rule-based fallback only)",
            "none": "",
        }[run.gemini_client],
    }[run.mode]
    integ = run.integrity
    out: List[str] = []
    out.append("# Evaluation Report - PL Capital AI Portfolio Analytics Agent\n")
    if run.question_subset:
        subset = ", ".join(f"Q{i}" for i in run.question_subset)
        out.append(f"> **Targeted subset run: {subset} only** ({run.total} of {run.ground_truth_total} ground-truth "
                   "questions). This is **not** a full evaluation; its score must not be combined with or presented "
                   "as a full-run score.\n")
        out.append(f"**Subset score: {run.passed}/{run.total}**\n")
    else:
        out.append(f"**Overall score: {run.passed}/{run.total}** ({run.passed / run.total * 100:.0f}%)\n")
    out.append("| Item | Value |\n|---|---|")
    out.append(f"| Evaluation mode | {mode_label} |")
    if run.question_subset:
        out.append(f"| Questions evaluated | {', '.join(f'Q{i}' for i in run.question_subset)} (subset) |")
    if run.mode == "llm":
        out.append(f"| Gemini model | `{run.model_name}` |")
    out.append(f"| Run started | {run.started_at} |")
    out.append(f"| Duration | {run.duration_s:.2f} s |")
    out.append(f"| Python | {platform.python_version()} ({platform.system()}) |")
    out.append(f"| Ground truth | `ground_truth_dataset.json` ({run.total} questions) |")
    out.append(f"| Database | `{Path(run.db_path).relative_to(PROJECT_ROOT) if Path(run.db_path).is_relative_to(PROJECT_ROOT) else run.db_path}` |")
    out.append(
        f"| Data integrity | {integ['db_total']} DB rows vs {integ['csv_total']} CSV rows across "
        f"{len(integ['tables'])} tables ({'match' if integ['matches'] else 'MISMATCH'}); "
        f"{integ['fk_violations']} foreign-key violations |"
    )
    out.append("")

    out.append("## Method\n")
    out.append("- **Q1-Q8 (text2sql):** the official `sql_query` from the ground truth is executed unchanged on an "
               "independent read-only `sqlite3` connection. The agent's answer must have the same row count and "
               "matching values (numeric tolerance: abs 1e-6, rel 1e-9). When the official query contains "
               "`ORDER BY`, row order must also match. Column aliases are not required to match.")
    out.append("- **Q9-Q10 (exposure_calculator):** expected exposures are recomputed from the original CSV files "
               "(`holdings`, `securities`, `sectors`, `portfolios`): `SUM(current_weight) x 100` per sector for "
               "`asset_type = 'Stock'` only, bonds excluded, **no renormalisation**. The agent must resolve the "
               f"ground-truth portfolio, return exactly these sectors (tolerance {EXPOSURE_TOL_PP} percentage points) "
               "and report the same number of excluded bond holdings.")
    out.append("- **Routing:** every question must be routed to the tool named by its ground-truth `type`.")
    out.append("- The agent is never used to compute expected results.\n")

    out.append("## Results Summary\n")
    out.append("| Q | Type | Difficulty | Expected tool | Selected tool | Generation | Rows (exp/act) | Result |")
    out.append("|---|---|---|---|---|---|---|---|")
    for r in run.results:
        gen = r.generation_method or "-"
        if r.fallback_used:
            gen += " (fallback)"
        out.append(
            f"| Q{r.qid} | {r.qtype} | {r.difficulty} | {r.expected_tool} | {r.selected_tool or '-'} | {gen} | "
            f"{len(r.expected_rows)}/{len(r.actual_rows)} | {'PASS' if r.passed else '**FAIL**'} |"
        )
    out.append("")

    failed = [r for r in run.results if not r.passed]
    out.append("## Failed Questions\n")
    if not failed:
        out.append("None. All questions passed.\n")
    for r in failed:
        out.append(f"### Q{r.qid}: {r.question}\n")
        for f in r.failures:
            out.append(f"- {f}")
        out.append("")

    out.append("## Question Details\n")
    for r in run.results:
        out.append(f"### Q{r.qid} - {'PASS' if r.passed else 'FAIL'}\n")
        out.append(f"**Question:** {r.question}\n")
        meta = [
            f"Selected tool: `{r.selected_tool}`",
            f"Agent status: `{r.agent_status}`",
            f"Generation method: `{r.generation_method}`",
        ]
        if r.model_name:
            meta.append(f"Model: `{r.model_name}`")
        if r.fallback_used:
            meta.append(f"Fallback reason: {r.fallback_reason}")
        meta.append(f"Agent time: {r.duration_ms:.1f} ms")
        if r.qtype == "text2sql":
            meta.append(f"Ordered comparison: {'yes' if r.ordered_comparison else 'no'}")
        out.extend(f"- {m}" for m in meta)
        out.append("")
        if r.generated_sql:
            out.append("Agent SQL:\n")
            out.append(f"```sql\n{r.generated_sql}\n```\n")
        out.append("Expected result:\n")
        out.append(_md_table(r.expected_columns, r.expected_rows) + "\n")
        out.append("Actual result:\n")
        out.append(_md_table(r.actual_columns, r.actual_rows) + "\n")
        if r.failures:
            out.append("Failure explanation:\n")
            out.extend(f"- {f}" for f in r.failures)
            out.append("")
        if r.notes:
            out.append("Notes:\n")
            out.extend(f"- {n}" for n in r.notes)
            out.append("")

    out.append("## Verification Status\n")
    out.extend(f"- {line}" for line in verification_status(run))
    out.append("")

    out.append("## Limitations\n")
    out.append("- The ground truth defines 10 questions. A perfect score shows the agent reproduces these answers; "
               "it does not prove correctness on unseen questions.")
    out.append("- In deterministic mode the rule-based generator emits SQL equivalent to the official queries for "
               "the supported question patterns; questions outside those patterns return `unsupported`.")
    out.append("- Gemini-generated SQL is non-deterministic. Results from `--use-llm` runs can vary between runs "
               "and model versions, and depend on API availability (timeouts fall back to rule-based SQL, which "
               "is reported per question).")
    out.append("- Comparison ignores column names and tolerates extra columns, but requires all expected values, "
               "the exact row count and, where the official query specifies `ORDER BY`, the same row order. "
               "Rows tied on the ORDER BY key could legitimately appear in a different order; none of the "
               "supplied questions currently rely on this.")
    out.append("- Q7 is evaluated with the official market-value definition (quantity x current_price, as a share "
               "of total portfolio value including bonds) and Q8 with the supplied diversification semantics "
               "(distinct Stock sectors / all holdings). These are intentionally different from the weight-based "
               "exposure calculation used for Q9-Q10.")
    out.append("")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────── CLI


def _load_env_for_llm() -> None:
    """Load .env only for --use-llm runs (skipped under the automated test suite)."""
    if os.environ.get("PORTFOLIO_AGENT_SKIP_DOTENV") == "1":
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def build_agent(db: PortfolioDatabase, use_llm: bool) -> Tuple[PortfolioAgent, str, Optional[str]]:
    """Return (agent, gemini_client_state, model_name)."""
    sql_tool = SQLQueryTool(db=db, use_gemini=use_llm)
    agent = PortfolioAgent(db=db, sql_tool=sql_tool)
    if not use_llm:
        return agent, "none", None
    gen = sql_tool.gemini_generator
    return agent, ("live" if gen.is_configured else "unconfigured"), gen.model_name


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evaluate.py", description="Evaluate the agent against the ground truth.")
    p.add_argument("--use-llm", action="store_true", help="Enable Gemini SQL generation (makes live API calls).")
    p.add_argument("--db", default=None, help="SQLite database path (default: database/portfolio.db).")
    p.add_argument("--ground-truth", default=str(DEFAULT_GROUND_TRUTH), help="Ground-truth JSON path.")
    p.add_argument("--data-dir", default=None, help="Directory with the original CSV files (default: data/).")
    p.add_argument("--report", default=None,
                   help="Markdown report output path (default: evaluation_report.md; "
                        "evaluation_report_subset.md when --questions is used).")
    p.add_argument("--no-report", action="store_true", help="Do not write the Markdown report.")
    p.add_argument("--questions", default=None,
                   help="Comma-separated ground-truth question IDs to evaluate (e.g. 7). Default: all.")
    return p


def select_questions(questions: List[Dict[str, Any]], spec: Optional[str]) -> Tuple[List[Dict[str, Any]], Optional[List[int]]]:
    """Return (selected questions, subset IDs or None). Raises ValueError on unknown/invalid IDs."""
    if not spec:
        return questions, None
    try:
        wanted = sorted({int(x) for x in spec.split(",") if x.strip()})
    except ValueError:
        raise ValueError("--questions must be comma-separated integers, e.g. --questions 7") from None
    known = {int(q["id"]) for q in questions}
    unknown = [i for i in wanted if i not in known]
    if not wanted or unknown:
        raise ValueError(f"Unknown question ID(s): {unknown or spec!r}. Valid IDs: {sorted(known)}")
    return [q for q in questions if int(q["id"]) in wanted], wanted


def print_summary(run: EvaluationRun) -> None:
    print("\nPL Capital AI Portfolio Analytics Agent - Evaluation")
    print(f"Mode: {run.mode}" + (f" (model {run.model_name}, gemini client: {run.gemini_client})" if run.mode == "llm" else ""))
    print("-" * 78)
    print(f"{'Q':<4}{'Type':<21}{'Tool':<21}{'Generation':<22}{'Result':<6}")
    print("-" * 78)
    for r in run.results:
        gen = (r.generation_method or "-") + (" (fallback)" if r.fallback_used else "")
        print(f"Q{r.qid:<3}{r.qtype:<21}{str(r.selected_tool):<21}{gen:<22}{'PASS' if r.passed else 'FAIL'}")
        for f in r.failures:
            print(f"      - {f}")
    print("-" * 78)
    print(f"Score: {run.passed}/{run.total}  ({run.duration_s:.2f} s)")
    for line in verification_status(run):
        print("Note: " + line.replace("**", ""))


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.use_llm:
        _load_env_for_llm()

    db = PortfolioDatabase(db_path=args.db)
    if not db.is_initialized():
        print(f"Database at {db.db_path} is missing or incomplete. Run: python init_db.py", file=sys.stderr)
        return 2
    try:
        data_dir = Path(args.data_dir).resolve() if args.data_dir else PortfolioDatabase.resolve_data_dir()
        all_questions = load_ground_truth(Path(args.ground_truth))
        try:
            questions, subset = select_questions(all_questions, args.questions)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        agent, gemini_client, model_name = build_agent(db, args.use_llm)
        run = run_evaluation(
            agent, questions, db.db_path, data_dir,
            mode="llm" if args.use_llm else "deterministic", gemini_client=gemini_client, model_name=model_name,
        )
        run.question_subset, run.ground_truth_total = subset, len(all_questions)
    finally:
        db.close()

    print_summary(run)
    if subset:
        print(f"Note: targeted subset run ({', '.join(f'Q{i}' for i in subset)}); not a full evaluation.")
    if not args.no_report:
        default = PROJECT_ROOT / ("evaluation_report_subset.md" if subset else DEFAULT_REPORT.name)
        report_path = Path(args.report) if args.report else default
        report_path.write_text(render_report(run), encoding="utf-8")
        print(f"Report written to {report_path}")
    return 0 if run.passed == run.total else 1


if __name__ == "__main__":
    sys.exit(main())

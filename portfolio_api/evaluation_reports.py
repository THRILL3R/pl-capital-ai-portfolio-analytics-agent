"""
Read the evaluator's saved Markdown reports and expose them as structured data.

The Evaluation screen is a *static view* of reports written by ``evaluate.py``. Nothing is
computed or invented here. Each report is classified by the mode recorded INSIDE the file
(not by its file name), so a deterministic run can never be shown as a live Gemini result:

    deterministic  - "Evaluation mode: Deterministic ..."
    live           - "Evaluation mode: Gemini-enabled ... (live API)", full run
    live_subset    - same, but a targeted subset run (``--questions``)
    other          - mocked / unconfigured / unrecognised (never shown as live evidence)

Which full live report is "current" is decided by an explicit preference, not by file name
alone or by timestamps: ``PREFERRED_LIVE_REPORT`` (the final live run) wins whenever it is a
valid full live report. Any other full live report is kept as a *historical* live run and is
reported separately, so an older, lower score can never be shown as the current result.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

PREFERRED_LIVE_REPORT = "docs/EVALUATION_FINAL_LIVE_20261109.md"

CANDIDATE_REPORTS = (
    PREFERRED_LIVE_REPORT,
    "evaluation_report.md",  # earlier (historical) full live run, kept for the record
    "evaluation_report_subset.md",
    "docs/EVALUATION_DETERMINISTIC.md",
    "docs/EVALUATION_Q7_TARGETED.md",
)

_SCORE_RE = re.compile(r"\*\*(Overall|Subset) score:\s*(\d+)\s*/\s*(\d+)\*\*")
_ITEM_RE = re.compile(r"^\|\s*([^|]+?)\s*\|\s*(.+?)\s*\|\s*$")
_QROW_RE = re.compile(r"^\|\s*Q(\d+)\s*\|(.+)\|\s*$")
_FAILED_HEAD_RE = re.compile(r"^###\s+Q(\d+):")


def parse_report(text: str) -> Dict[str, Any]:
    """Parse one evaluator report. Missing pieces are returned as None / empty."""
    score = re.search(_SCORE_RE, text)
    items: Dict[str, str] = {}
    for line in text.splitlines():
        m = _ITEM_RE.match(line)
        if m and m.group(1) not in ("Item", "---"):
            items.setdefault(m.group(1).strip(), m.group(2).strip())

    mode_text = items.get("Evaluation mode", "")
    is_subset = bool(score and score.group(1) == "Subset") or "Targeted subset run" in text
    if mode_text.startswith("Deterministic"):
        kind = "deterministic"
    elif "(live API)" in mode_text:
        kind = "live_subset" if is_subset else "live"
    else:
        kind = "other"

    results: Dict[int, Dict[str, Any]] = {}
    in_summary = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_summary = line.strip() == "## Results Summary"
            continue
        if not in_summary:
            continue
        m = _QROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 7:
            continue
        # | Q | Type | Difficulty | Expected tool | Selected tool | Generation | Rows | Result |
        results[int(m.group(1))] = {
            "type": cells[0],
            "expected_tool": cells[2],
            "selected_tool": cells[3],
            "generation": cells[4],
            "rows": cells[5],
            "result": "PASS" if cells[6].replace("*", "").strip().upper() == "PASS" else "FAIL",
        }

    failures: Dict[int, List[str]] = {}
    current: Optional[int] = None
    in_failed = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_failed = line.strip() == "## Failed Questions"
            current = None
            continue
        if not in_failed:
            continue
        h = _FAILED_HEAD_RE.match(line)
        if h:
            current = int(h.group(1))
            failures[current] = []
        elif current is not None and line.startswith("- "):
            failures[current].append(line[2:].strip())

    model = items.get("Gemini model", "").strip("`") or None
    return {
        "kind": kind,
        "mode": mode_text or None,
        "score": int(score.group(2)) if score else None,
        "total": int(score.group(3)) if score else None,
        "model": model,
        "started": items.get("Run started"),
        "results": results,
        "failures": failures,
    }


def _load_ground_truth(root: Path) -> List[Dict[str, Any]]:
    try:
        data = json.loads((root / "ground_truth_dataset.json").read_text(encoding="utf-8"))
        return [
            {"id": int(q["id"]), "type": q.get("type"), "question": q.get("question")}
            for q in data.get("questions", [])
        ]
    except (OSError, ValueError, KeyError):
        return []


def load_evaluation(root: Path) -> Dict[str, Any]:
    """Combine ground-truth question text with the newest report of each kind."""
    root = Path(root)
    best: Dict[str, Dict[str, Any]] = {}
    live_reports: List[Dict[str, Any]] = []
    for rel in CANDIDATE_REPORTS:
        path = root / rel
        try:
            parsed = parse_report(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        parsed["source"] = rel
        kind = parsed["kind"]
        if kind == "other":
            continue
        if kind == "live":
            live_reports.append(parsed)
            continue
        if kind not in best or (parsed["started"] or "") > (best[kind]["started"] or ""):
            best[kind] = parsed

    # Full live runs: the preferred (final) report is current whenever it is a valid full live
    # report; otherwise the newest full live report stands in. Every other full live report is
    # historical and is never the current result.
    live: Optional[Dict[str, Any]] = None
    historical: Optional[Dict[str, Any]] = None
    if live_reports:
        preferred = [r for r in live_reports if r["source"] == PREFERRED_LIVE_REPORT]
        live = preferred[0] if preferred else max(live_reports, key=lambda r: r["started"] or "")
        others = [r for r in live_reports if r is not live]
        historical = max(others, key=lambda r: r["started"] or "") if others else None
    if live is not None:
        live["role"] = "final" if live["source"] == PREFERRED_LIVE_REPORT else "latest"
    if historical is not None:
        historical["role"] = "historical"

    def slim(report: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not report:
            return None
        meta = {k: report[k] for k in ("source", "mode", "score", "total", "model", "started")}
        meta["role"] = report.get("role")
        return meta

    questions: List[Dict[str, Any]] = []
    det = best.get("deterministic")
    sub = best.get("live_subset")
    for gt in _load_ground_truth(root):
        qid = gt["id"]
        entry: Dict[str, Any] = {
            "id": qid,
            "question": gt["question"],
            "type": gt["type"],
            "deterministic": det["results"].get(qid) if det else None,
            "live": live["results"].get(qid) if live else None,
            "live_failures": (live["failures"].get(qid, []) if live else []),
            "historical_live": historical["results"].get(qid) if historical else None,
            "historical_live_failures": (historical["failures"].get(qid, []) if historical else []),
            "targeted": sub["results"].get(qid) if sub else None,
        }
        questions.append(entry)

    live_answers = [e["live"] for e in questions if e["live"] and e["live"]["type"] == "text2sql"]
    return {
        "deterministic": slim(det),
        "live": slim(live),
        "historical_live": slim(historical),
        "live_subset": slim(sub),
        "live_generation": {
            "sql_questions": len(live_answers),
            "by_gemini": sum(1 for r in live_answers if r["generation"] == "gemini"),
            "fallbacks": sum(1 for r in live_answers if "fallback" in r["generation"]),
        } if live else None,
        "questions": questions,
    }

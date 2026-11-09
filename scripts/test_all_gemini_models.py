#!/usr/bin/env python3
"""
Gemini model discovery and benchmark (direct Google Gemini Developer API).

Standalone tool. It does NOT touch the production integration in ``portfolio_agent/``
and never edits ``.env``. The API key is read from the environment or the project's
``.env`` file and is never printed, logged or written to any output file.

Sub-commands (run from the project root with the project venv):

    .venv\\Scripts\\python.exe scripts\\test_all_gemini_models.py discover
        List every model returned by the API, classify it and print the ranked inventory.
        One API call (models.list); no generation requests.

    .venv\\Scripts\\python.exe scripts\\test_all_gemini_models.py run
        Discover, then send ONE generation request to each eligible free-tier text model,
        sequentially, strongest first. No retries, no concurrency. Stops on a genuine
        quota-exhaustion error. Writes docs/GEMINI_MODEL_RESULTS.csv and
        docs/GEMINI_MODEL_BENCHMARK.md.

    .venv\\Scripts\\python.exe scripts\\test_all_gemini_models.py report [--assessments FILE]
        Re-render the Markdown report from the CSV (no API calls). An optional JSON file
        adds the reviewer's subjective quality scores and recommendation text.

Exit codes: 0 = ok, 1 = a step failed, 2 = not configured.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = PROJECT_ROOT / "docs"
DEFAULT_CSV = DOCS_DIR / "GEMINI_MODEL_RESULTS.csv"
DEFAULT_MD = DOCS_DIR / "GEMINI_MODEL_BENCHMARK.md"

TEST_PROMPT = (
    "You are an AI portfolio analytics assistant. Explain how to calculate Technology sector "
    "exposure in an investment portfolio. Reply with a concise, technically correct answer."
)
MAX_OUTPUT_TOKENS = 1024  # short limit; includes thinking tokens on thinking models
TIMEOUT_SECONDS = 60
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing (redirects to https://aistudio.google.com/docs/pricing)"
PRICING_CHECKED = "2026-11-09"

# --------------------------------------------------------------------------------------
# Pricing evidence: transcribed from the official pricing page (Standard tier, "Free Tier"
# column for input/output text tokens) on PRICING_CHECKED. A model ID that is not listed
# here is treated as UNVERIFIED and is never sent a generation request.
# --------------------------------------------------------------------------------------
PRICING_EVIDENCE: dict[str, dict[str, str]] = {
    # free of charge (Standard tier)
    "gemini-3.8-flash": {"free_tier": "free", "paid": "$0.75 in / $3.75 out per 1M (to 2026-12-31)"},
    "gemini-3.6-flash": {"free_tier": "free", "paid": "$0.75 in / $3.75 out per 1M (to 2026-12-31)"},
    "gemini-3.5-flash-lite": {"free_tier": "free", "paid": "$0.30 in / $2.50 out per 1M"},
    "gemini-3.1-flash-lite": {"free_tier": "free", "paid": "$0.25 in / $1.50 out per 1M"},
    "gemini-3-flash-preview": {"free_tier": "free", "paid": "$0.50 in / $3.00 out per 1M"},
    "gemini-2.5-pro": {"free_tier": "free", "paid": "$1.25 in / $10.00 out per 1M (deprecated; routes to gemini-3.8-flash from 2026-12-01)"},
    "gemini-2.5-flash": {"free_tier": "free", "paid": "$0.30 in / $2.50 out per 1M (deprecated; routes to gemini-3.5-flash-lite from 2026-12-01)"},
    "gemini-2.5-flash-lite": {"free_tier": "free", "paid": "$0.10 in / $0.40 out per 1M"},
    # paid only (Free Tier "Not available")
    "gemini-4-argon": {"free_tier": "paid", "paid": "$2.00 in / $10.00 out per 1M (to 2026-11-30)"},
    "gemini-3.1-pro-preview": {"free_tier": "paid", "paid": "$2.00 in / $12.00 out per 1M (<=200k)"},
    "gemini-3.1-pro-preview-customtools": {"free_tier": "paid", "paid": "as gemini-3.1-pro-preview"},
}

# Substrings marking models whose primary task is not plain text generation.
INCOMPATIBLE_MARKERS = (
    "embedding", "imagen", "veo", "tts", "image", "audio", "live", "transcribe", "translate",
    "lyria", "omni", "robotics", "aqa", "computer-use", "banana", "customtools",
)

TIER_LABELS = {
    1: "Flagship / advanced reasoning",
    2: "Pro",
    3: "High-capability Flash",
    4: "Flash Preview",
    5: "Flash-Lite",
    6: "Older compatible Gemini",
    9: "Other / unclassified",
}


# --------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------
@dataclass
class ModelRecord:
    rank: int = 0
    model_id: str = ""
    display_name: str = ""
    description: str = ""
    supported_actions: str = ""
    input_token_limit: str = ""
    output_token_limit: str = ""
    compatible: bool = False
    exclusion_reason: str = ""
    tier: int = 9
    tier_label: str = ""
    free_tier: str = "unverified"  # free | paid | unverified
    paid_price: str = ""
    test_status: str = "not_tested"  # success | failed | skipped_paid | skipped_unverified | skipped_quota_stop | not_tested
    error_category: str = ""
    http_code: str = ""
    error_message: str = ""
    response_time_s: str = ""
    finish_reason: str = ""
    output_tokens: str = ""
    thinking_tokens: str = ""
    answer: str = ""
    quality_score: str = ""  # reviewer assessment (subjective), 1-5
    quality_notes: str = ""


CSV_FIELDS = [f.name for f in fields(ModelRecord)]


# --------------------------------------------------------------------------------------
# Secrets handling
# --------------------------------------------------------------------------------------
def load_api_key() -> str:
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


_KEY_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"AQ\.[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(key=)[^&\s\"']+", re.IGNORECASE),
]


def redact(text: str, key: str) -> str:
    text = str(text)
    if key:
        text = text.replace(key, "[REDACTED]")
    for pattern in _KEY_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
    return text


# --------------------------------------------------------------------------------------
# Classification and ranking
# --------------------------------------------------------------------------------------
def _version(model_id: str) -> float:
    match = re.search(r"gemini-(\d+(?:\.\d+)?)", model_id)
    return float(match.group(1)) if match else 0.0


def classify(model_id: str, actions: list[str]) -> tuple[bool, str, int]:
    mid = model_id.lower()
    if mid.startswith("gemma"):
        return False, "Gemma open model (not Gemini)", 9
    if not mid.startswith("gemini"):
        return False, "agent / research product, not a gemini-* model", 9
    if "generateContent" not in actions:
        return False, "does not support generateContent", 9
    for marker in INCOMPATIBLE_MARKERS:
        if marker in mid:
            return False, f"specialised task model ('{marker}')", 9
    version = _version(mid)
    if "ultra" in mid or "deep-think" in mid or (version >= 4 and "flash" not in mid):
        tier = 1
    elif "pro" in mid:
        tier = 2
    elif "flash-lite" in mid:
        tier = 5
    elif "flash" in mid and ("preview" in mid or "exp" in mid):
        tier = 4
    elif "flash" in mid:
        tier = 3
    else:
        tier = 9
    if version and version < 3 and tier in (2, 3, 4, 5):
        tier = 6  # older generation
    return True, "", tier


def rank_records(records: list[ModelRecord]) -> list[ModelRecord]:
    def key(rec: ModelRecord):
        mid = rec.model_id
        family = 0 if "pro" in mid else 2 if "flash-lite" in mid else 1
        return (0 if rec.compatible else 1, rec.tier, -_version(mid), family, mid)

    ordered = sorted(records, key=key)
    for idx, rec in enumerate(ordered, start=1):
        rec.rank = idx
        rec.tier_label = TIER_LABELS.get(rec.tier, "")
    return ordered


# --------------------------------------------------------------------------------------
# API access
# --------------------------------------------------------------------------------------
def make_client(api_key: str):
    from google import genai
    from google.genai import types

    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            timeout=int(TIMEOUT_SECONDS * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),  # no retries
        ),
    )


def discover(client) -> list[ModelRecord]:
    records: list[ModelRecord] = []
    for model in client.models.list(config={"page_size": 100}):
        model_id = (model.name or "").removeprefix("models/")
        actions = list(model.supported_actions or [])
        compatible, reason, tier = classify(model_id, actions)
        evidence = PRICING_EVIDENCE.get(model_id, {})
        records.append(
            ModelRecord(
                model_id=model_id,
                display_name=model.display_name or "",
                description=(model.description or "").replace("\n", " ").strip(),
                supported_actions=";".join(actions),
                input_token_limit=str(model.input_token_limit or ""),
                output_token_limit=str(model.output_token_limit or ""),
                compatible=compatible,
                exclusion_reason=reason,
                tier=tier,
                free_tier=evidence.get("free_tier", "unverified"),
                paid_price=evidence.get("paid", ""),
            )
        )
    return rank_records(records)


def classify_error(exc: Exception) -> tuple[str, str]:
    code = getattr(exc, "code", None)
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if code == 429:
        if "limit: 0" in text or "limit:0" in text:
            return "quota_not_available_for_project", "429"
        return "quota_exhausted", "429"
    mapping = {400: "bad_request", 401: "auth", 403: "permission_denied", 404: "model_not_found",
               500: "server_error", 503: "unavailable", 504: "deadline_exceeded"}
    if isinstance(code, int):
        return mapping.get(code, "api_error"), str(code)
    if "timeout" in name or "timed out" in text or "deadline" in text:
        return "client_timeout", ""
    if "connect" in name or "connection" in text:
        return "network", ""
    return "unexpected_error", ""


def test_model(client, rec: ModelRecord, api_key: str) -> None:
    from google.genai import types

    config = types.GenerateContentConfig(max_output_tokens=MAX_OUTPUT_TOKENS)
    started = time.perf_counter()
    try:
        response = client.models.generate_content(model=rec.model_id, contents=TEST_PROMPT, config=config)
    except Exception as exc:  # recorded, never retried
        rec.response_time_s = f"{time.perf_counter() - started:.2f}"
        rec.test_status = "failed"
        rec.error_category, rec.http_code = classify_error(exc)
        rec.error_message = redact(str(exc), api_key)[:300]
        return
    rec.response_time_s = f"{time.perf_counter() - started:.2f}"
    candidates = getattr(response, "candidates", None) or []
    if candidates and getattr(candidates[0], "finish_reason", None) is not None:
        rec.finish_reason = str(getattr(candidates[0].finish_reason, "name", candidates[0].finish_reason))
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        rec.output_tokens = str(getattr(usage, "candidates_token_count", "") or "")
        rec.thinking_tokens = str(getattr(usage, "thoughts_token_count", "") or "")
    try:
        text = (response.text or "").strip()
    except Exception:
        text = ""
    if text:
        rec.test_status = "success"
        rec.answer = text
    else:
        rec.test_status = "failed"
        rec.error_category = "empty_response"
        rec.error_message = f"no text returned (finish_reason={rec.finish_reason or 'unknown'})"


def run_tests(client, records: list[ModelRecord], api_key: str) -> None:
    stop = False
    for rec in records:
        if not rec.compatible:
            rec.test_status = "excluded"
            continue
        if rec.free_tier == "paid":
            rec.test_status = "skipped_paid"
            continue
        if rec.free_tier != "free":
            rec.test_status = "skipped_unverified"
            continue
        if stop:
            rec.test_status = "skipped_quota_stop"
            continue
        print(f"  [{rec.rank:>2}] {rec.model_id:<32} ... ", end="", flush=True)
        test_model(client, rec, api_key)
        detail = rec.error_category if rec.test_status == "failed" else f"{len(rec.answer)} chars"
        print(f"{rec.test_status} ({rec.response_time_s}s) {detail}")
        if rec.error_category == "quota_exhausted":
            print("  Quota limit reached - stopping further tests.")
            stop = True


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------
def write_csv(records: list[ModelRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for rec in records:
            writer.writerow(asdict(rec))


def read_csv(path: Path) -> list[ModelRecord]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for row in rows:
        rec = ModelRecord(**{k: row.get(k, "") for k in CSV_FIELDS})
        rec.rank = int(rec.rank or 0)
        rec.tier = int(rec.tier or 9)
        rec.compatible = str(rec.compatible) == "True"
        out.append(rec)
    return out


def _md(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(records: list[ModelRecord], meta: dict, assessments: dict) -> str:
    compatible = [r for r in records if r.compatible]
    tested = [r for r in compatible if r.test_status in ("success", "failed")]
    ok = [r for r in tested if r.test_status == "success"]
    failed = [r for r in tested if r.test_status == "failed"]
    skipped = [r for r in compatible if r.test_status.startswith("skipped")]
    L: list[str] = []
    L.append("# Gemini Model Discovery & Benchmark\n")
    L.append(f"Run date: {meta.get('run_date', '')} · API: Gemini Developer API (direct key, `google-genai` "
             f"{meta.get('sdk_version', '')}) · Generated by `scripts/test_all_gemini_models.py`.\n")
    L.append("No API key appears in this report. Only the official Gemini API was used (no gateway).\n")
    L.append("## Summary\n")
    L.append("| Metric | Count |\n|---|---:|")
    L.append(f"| Models discovered (`models.list`) | {len(records)} |")
    L.append(f"| Compatible text-generation Gemini models | {len(compatible)} |")
    L.append(f"| Free-tier models tested | {len(tested)} |")
    L.append(f"| Successful tests | {len(ok)} |")
    L.append(f"| Failed tests | {len(failed)} |")
    L.append(f"| Skipped (paid-only or unverified free tier) | {len(skipped)} |\n")

    L.append("## Method\n")
    L.append(f"- **Prompt (identical for all models):** \"{TEST_PROMPT}\"")
    L.append(f"- One request per model, sequential, strongest first; `max_output_tokens={MAX_OUTPUT_TOKENS}` "
             f"(thinking tokens count toward it); client timeout {TIMEOUT_SECONDS}s; SDK retries disabled.")
    L.append("- Stop rule: a `429` that indicates exhausted quota stops all further tests. A `429` with "
             "`limit: 0` means the model has no free quota for this project and is recorded, not treated as exhaustion.")
    L.append(f"- **Free-tier evidence:** {PRICING_SOURCE}, Standard tier, checked {PRICING_CHECKED}. "
             "Models absent from that page are *unverified* and were not sent generation requests.")
    L.append("- Response time is wall-clock for one request from this machine; it is a single sample, not a latency benchmark.\n")

    L.append("## Results\n")
    L.append("Factual columns come from the API. **Quality** is the reviewer's subjective 1–5 score "
             "(technical correctness, financial terminology, clarity, completeness, instruction following); "
             "it is not an API measurement.\n")
    L.append("| Rank | Model ID | Tier | Free Tier | Generation | Response Time | Quality |\n|---:|---|---|---|---|---:|---|")
    for r in compatible:
        gen = r.test_status
        if r.test_status == "failed":
            gen = f"failed: {r.error_category}" + (f" (HTTP {r.http_code})" if r.http_code else "")
        rt = f"{r.response_time_s}s" if r.response_time_s else "—"
        q = r.quality_score or "—"
        L.append(f"| {r.rank} | `{r.model_id}` | {r.tier_label} | {r.free_tier} | {gen} | {rt} | {q} |")
    L.append("")

    if failed:
        L.append("## Failed tests — error explanations\n")
        explain = {
            "deadline_exceeded": "Server-side deadline exceeded (HTTP 504): the model did not answer in time for this project.",
            "client_timeout": f"No response within the {TIMEOUT_SECONDS}s client timeout.",
            "quota_not_available_for_project": "HTTP 429 with `limit: 0`: no free-tier quota for this model on this project.",
            "quota_exhausted": "HTTP 429: quota exhausted; testing stopped.",
            "model_not_found": "HTTP 404: the model appears in models.list but is not callable for this key "
                               "(Google's message: no longer available to new users).",
            "permission_denied": "HTTP 403: the key/project is not permitted to use this model.",
            "unavailable": "HTTP 503: model overloaded / temporarily unavailable.",
            "empty_response": "Request succeeded but returned no text (often all tokens spent on thinking).",
        }
        L.append("| Model ID | Category | Explanation | Redacted API message (truncated) |\n|---|---|---|---|")
        for r in failed:
            L.append(f"| `{r.model_id}` | {r.error_category} | {explain.get(r.error_category, 'See message.')} | {_md(r.error_message[:160])} |")
        L.append("")

    if skipped:
        L.append("## Skipped (no generation request sent)\n")
        L.append("| Model ID | Reason | Paid price (Standard) |\n|---|---|---|")
        for r in skipped:
            reason = {"skipped_paid": "Paid-only (Free Tier: \"Not available\")",
                      "skipped_unverified": "Free-tier status not on the official pricing page",
                      "skipped_quota_stop": "Not reached: quota stop"}.get(r.test_status, r.test_status)
            L.append(f"| `{r.model_id}` | {reason} | {r.paid_price or '—'} |")
        L.append("")

    if assessments.get("recommendation"):
        L.append("## Recommendation (reviewer assessment)\n")
        L.append(assessments["recommendation"].strip() + "\n")

    if ok:
        L.append("## Generated answers\n")
        for r in ok:
            notes = f" — reviewer notes: {r.quality_notes}" if r.quality_notes else ""
            L.append(f"<details><summary><code>{r.model_id}</code> · {r.response_time_s}s · "
                     f"{r.output_tokens or '?'} output tokens{(' + ' + r.thinking_tokens + ' thinking') if r.thinking_tokens else ''}"
                     f" · finish {r.finish_reason or '?'}{_md(notes)}</summary>\n")
            L.append(r.answer.strip() + "\n\n</details>\n")

    L.append("## Complete discovered model inventory\n")
    L.append("| # | Model ID | Display name | Supported actions | Compatible | Exclusion reason | Free tier |\n|---:|---|---|---|---|---|---|")
    for r in records:
        L.append(f"| {r.rank} | `{r.model_id}` | {_md(r.display_name)} | {_md(r.supported_actions.replace(';', ', '))} | "
                 f"{'yes' if r.compatible else 'no'} | {_md(r.exclusion_reason) or '—'} | "
                 f"{r.free_tier if r.compatible else 'n/a (not tested)'} |")
    L.append("")
    L.append("<details><summary>Model descriptions (as returned by the API)</summary>\n")
    for r in records:
        if r.description:
            L.append(f"- `{r.model_id}`: {_md(r.description)}")
    L.append("\n</details>\n")
    return "\n".join(L)


def _sdk_version() -> str:
    try:
        from importlib.metadata import version

        return version("google-genai")
    except Exception:
        return "unknown"


def load_assessments(path: str | None, records: list[ModelRecord]) -> dict:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for rec in records:
        entry = data.get("models", {}).get(rec.model_id)
        if entry:
            rec.quality_score = str(entry.get("score", ""))
            rec.quality_notes = entry.get("notes", "")
    return data


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------
def print_inventory(records: list[ModelRecord]) -> None:
    print(f"\n{'#':>3}  {'model id':<44} {'compat':<6} {'tier':<30} free_tier")
    for r in records:
        print(f"{r.rank:>3}  {r.model_id:<44} {'yes' if r.compatible else 'no':<6} "
              f"{(r.tier_label if r.compatible else r.exclusion_reason)[:30]:<30} {r.free_tier}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["discover", "run", "report"])
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--md", default=str(DEFAULT_MD))
    parser.add_argument("--assessments", help="JSON with reviewer quality scores / recommendation (report only)")
    args = parser.parse_args(argv)

    if args.command == "report":
        records = read_csv(Path(args.csv))
        assessments = load_assessments(args.assessments, records)
        meta = {"run_date": assessments.get("run_date", "see CSV timestamp"), "sdk_version": _sdk_version()}
        write_csv(records, Path(args.csv))
        Path(args.md).write_text(render_markdown(records, meta, assessments), encoding="utf-8")
        print(f"Report written: {args.md}")
        return 0

    api_key = load_api_key()
    print(f"GEMINI_API_KEY: {'present' if api_key else 'MISSING'}")
    if not api_key:
        print("Not configured. Add GEMINI_API_KEY to .env and re-run.")
        return 2
    client = make_client(api_key)
    try:
        records = discover(client)
    except Exception as exc:
        category, code = classify_error(exc)
        print(f"Discovery failed [{category}{' ' + code if code else ''}]: {redact(str(exc), api_key)[:300]}")
        return 1
    print(f"Discovered {len(records)} models; {sum(r.compatible for r in records)} compatible text models.")
    print_inventory(records)
    if args.command == "discover":
        return 0

    print("\nSequential generation tests (one request each, no retries):")
    run_tests(client, records, api_key)
    write_csv(records, Path(args.csv))
    meta = {"run_date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "sdk_version": _sdk_version()}
    Path(args.md).write_text(render_markdown(records, meta, {}), encoding="utf-8")
    print(f"\nWrote {args.csv}\nWrote {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

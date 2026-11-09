#!/usr/bin/env python3
"""
Standalone text-to-SQL test of gemini-4-argon via the 1min.AI gateway.

- Uses ONLY ONEMIN_API_KEY and https://api.1min.ai/api/chat-with-ai (GEMINI_API_KEY is not used).
- Sends exactly ONE generation request (no retries).
- The schema context is introspected from the real project database (database/portfolio.db).
- Model SQL is never executed raw: it is validated with the project's validate_read_only_sql()
  and executed only through PortfolioDatabase.execute_read_only() (mode=ro, query_only,
  SQLite authorizer).
- API keys are never printed; any echoed error text is redacted.

Usage (from the project root):
    .venv\\Scripts\\python.exe scripts\\test_argon_portfolio.py

Exit codes: 0 = correct answer, 1 = failure / incorrect, 2 = not configured.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.database import (  # noqa: E402
    PortfolioDatabase,
    ReadOnlyQueryError,
    validate_read_only_sql,
)

ENDPOINT = "https://api.1min.ai/api/chat-with-ai"
MODEL_ID = "gemini-4-argon"
QUESTION = "How many portfolios do we have in total?"
EXPECTED_ANSWER = 13
REQUEST_TIMEOUT_S = 300


def load_onemin_key() -> str:
    try:
        from dotenv import dotenv_values

        # Read only the one variable needed; nothing is exported to os.environ.
        value = dotenv_values(PROJECT_ROOT / ".env").get("ONEMIN_API_KEY")
    except ImportError:
        value = None
    return (value or os.environ.get("ONEMIN_API_KEY") or "").strip()


def redact(text: str, *secrets: str) -> str:
    text = str(text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED]", text)
    return re.sub(r"AQ\.[0-9A-Za-z_\-]{20,}", "[REDACTED]", text)


def build_prompt(schema: str) -> str:
    return (
        "You are a text-to-SQL engine for a SQLite portfolio analytics database.\n"
        "Use ONLY the tables and columns in the schema below.\n\n"
        f"{schema}\n\n"
        f"Question: {QUESTION}\n\n"
        "Return ONLY one valid, read-only SQLite SELECT statement. "
        "No explanation, no comments, no markdown, no code fences."
    )


def extract_sql(text: str) -> str:
    """Pull the SQL out of the model reply without altering it beyond trimming wrappers."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        return fenced.group(1).strip()
    return text


def main() -> int:
    print("=" * 64)
    print("PL Capital - gemini-4-argon text-to-SQL test (1min.AI gateway)")
    print("=" * 64)

    api_key = load_onemin_key()
    print(f"ONEMIN_API_KEY : {'present' if api_key else 'MISSING'}")
    print(f"Endpoint       : {ENDPOINT}")
    print(f"Model ID       : {MODEL_ID}")
    print(f"Question       : {QUESTION}")
    if not api_key:
        print("\nNot configured: ONEMIN_API_KEY is not set in .env.")
        return 2

    db = PortfolioDatabase()
    if not db.is_initialized():
        print("\nDatabase not initialised. Run: python init_db.py")
        return 1
    schema = db.table_schema()
    print(f"Schema context : {schema.count('Table: ')} tables introspected from {db.db_path.name}")

    # ---- the single generation request -------------------------------------------------
    print("\n[1] API request (single attempt, no retries)")
    started = time.perf_counter()
    try:
        response = requests.post(
            ENDPOINT,
            headers={"API-KEY": api_key, "Content-Type": "application/json"},
            json={
                "type": "UNIFY_CHAT_WITH_AI",
                "model": MODEL_ID,
                "promptObject": {"prompt": build_prompt(schema)},
            },
            timeout=REQUEST_TIMEOUT_S,
        )
    except requests.Timeout:
        print(f"    API connection : FAILED - timeout after {time.perf_counter() - started:.2f}s")
        db.close()
        return 1
    except requests.RequestException as exc:
        print(f"    API connection : FAILED - {type(exc).__name__}: {redact(exc, api_key)[:300]}")
        db.close()
        return 1
    elapsed = time.perf_counter() - started
    print(f"    HTTP status    : {response.status_code}")
    print(f"    Response time  : {elapsed:.2f}s")

    if not response.ok:
        body = redact(response.text, api_key)[:600]
        hint = ""
        if response.status_code in (402, 403) or re.search(r"credit|insufficient|balance", body, re.I):
            hint = " (looks like a credit / plan error)"
        elif response.status_code == 429:
            hint = " (rate limited)"
        elif response.status_code == 401:
            hint = " (authentication error)"
        print(f"    API connection : FAILED{hint}")
        print(f"    Error body     : {body}")
        db.close()
        return 1

    try:
        data = response.json()
    except ValueError:
        print("    API connection : OK, but the response is not JSON")
        db.close()
        return 1
    record = data.get("aiRecord") or {}
    result = (record.get("aiRecordDetail") or {}).get("resultObject")
    reply = result[0] if isinstance(result, list) and result else result
    if not isinstance(reply, str) or not reply.strip():
        print("    API connection : OK, but no text in aiRecord.aiRecordDetail.resultObject")
        print(f"    Top-level keys : {list(data.keys())}")
        db.close()
        return 1
    print("    API connection : OK")
    print(f"    Model reported : {record.get('model', 'n/a')}")
    for key in ("status", "credit", "credits", "creditUsed"):
        if key in record:
            print(f"    {key:<15}: {record[key]}")

    print("\n[2] Raw model response")
    print("    " + reply.strip().replace("\n", "\n    "))

    # ---- extraction + safety validation ------------------------------------------------
    sql = extract_sql(reply)
    print("\n[3] Generated SQL")
    print("    " + sql.replace("\n", "\n    "))

    print("\n[4] SQL safety validation (validate_read_only_sql)")
    try:
        cleaned = validate_read_only_sql(sql)
    except ReadOnlyQueryError as exc:
        print(f"    REJECTED: {exc}")
        print("    The SQL was NOT executed.")
        db.close()
        return 1
    print("    ACCEPTED: single read-only SELECT/WITH statement")

    # ---- execution through the guarded read-only path ----------------------------------
    print("\n[5] Execution (PortfolioDatabase.execute_read_only: mode=ro, query_only, authorizer)")
    try:
        outcome = db.execute_read_only(cleaned)
    except ReadOnlyQueryError as exc:
        print(f"    FAILED: {exc}")
        db.close()
        return 1
    print(f"    Columns  : {outcome['columns']}")
    print(f"    Rows     : {outcome['rows']}")
    print(f"    Row count: {outcome['row_count']}")
    db.close()

    # ---- correctness -------------------------------------------------------------------
    print(f"\n[6] Correctness vs ground truth (expected {EXPECTED_ANSWER})")
    rows = outcome["rows"]
    value = list(rows[0].values())[0] if len(rows) == 1 and len(rows[0]) == 1 else None
    correct = value is not None and str(value).strip() == str(EXPECTED_ANSWER)
    print(f"    Returned : {value if value is not None else rows}")
    print(f"    Verdict  : {'CORRECT' if correct else 'INCORRECT'}")

    print("\n" + "=" * 64)
    print(f"RESULT: {'PASS' if correct else 'FAIL'}  |  {MODEL_ID}  |  {elapsed:.2f}s  |  1 API request")
    print("=" * 64)
    return 0 if correct else 1


if __name__ == "__main__":
    sys.exit(main())

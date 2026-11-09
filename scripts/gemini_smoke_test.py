#!/usr/bin/env python3
"""
Live Gemini smoke test for the PL Capital AI Portfolio Analytics Agent.

This script makes REAL Gemini API requests (a few small calls). It is deliberately
separate from the automated unittest suite, which only uses mocks.

Usage (from the project root):
    .venv\\Scripts\\python.exe scripts\\gemini_smoke_test.py

Reads GEMINI_API_KEY / GEMINI_MODEL from the environment or the project's .env file.
The API key is never printed.

Exit codes: 0 = all checks passed, 1 = a check failed, 2 = not configured.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=False)
except ImportError:
    print("note: python-dotenv not installed; reading environment variables only.")

from portfolio_agent.database import PortfolioDatabase  # noqa: E402
from portfolio_agent.sql_generator import GeminiGenerationError, GeminiSQLGenerator  # noqa: E402
from portfolio_agent.tools import SQLQueryTool  # noqa: E402

# (question, check) pairs; checks compare against the official ground truth values.
SQL_CHECKS = [
    ("How many portfolios do we have in total?", lambda rows: len(rows) == 1 and list(rows[0].values())[0] == 13),
    (
        "What is the total Assets Under Management (AUM) for portfolios with high target risk level?",
        lambda rows: len(rows) == 1 and abs(float(list(rows[0].values())[0]) - 85_000_000) < 0.01,
    ),
]


def main() -> int:
    key_present = bool((os.environ.get("GEMINI_API_KEY") or "").strip())
    print("PL Capital - Gemini live smoke test")
    print(f"  GEMINI_API_KEY : {'present' if key_present else 'MISSING'}")
    if not key_present:
        print("\nNot configured. Add GEMINI_API_KEY to the .env file in the project root and re-run.")
        return 2

    db = PortfolioDatabase()
    if not db.is_initialized():
        print("\nDatabase not initialised. Run: python init_db.py")
        return 1

    generator = GeminiSQLGenerator(db=db)
    print(f"  Model          : {generator.model_name}")
    print(f"  Timeout        : {generator.timeout_seconds:.0f}s, max attempts {generator.max_attempts}")
    failures = 0

    # 1. Raw connectivity: minimal request, usable text back.
    print("\n[1] Raw generate_content request")
    try:
        client = generator._get_client()
        started = time.perf_counter()
        response = client.models.generate_content(
            model=generator.model_name, contents="Reply with exactly the word: OK"
        )
        elapsed = time.perf_counter() - started
        text = (getattr(response, "text", None) or "").strip()
        if text:
            print(f"    PASS  ({elapsed:.2f}s) response text: {text[:80]!r}")
        else:
            print("    FAIL  request succeeded but returned no text")
            failures += 1
    except GeminiGenerationError as exc:
        print(f"    FAIL  [{exc.category}] {exc}")
        db.close()
        return 1
    except Exception as exc:
        category = generator._classify_exception(exc)
        print(f"    FAIL  [{category}] {generator._sanitize(exc)}")
        db.close()
        return 1

    # 2. Structured SQL generation + read-only execution through SQLQueryTool.
    tool = SQLQueryTool(db=db, gemini_generator=generator, use_gemini=True)
    for idx, (question, check) in enumerate(SQL_CHECKS, start=2):
        print(f"\n[{idx}] SQLQueryTool via Gemini: {question}")
        started = time.perf_counter()
        res = tool.execute(question)
        elapsed = time.perf_counter() - started
        print(f"    generation_method : {res['generation_method']}  (model: {res['model_name']})")
        print(f"    fallback_used     : {res['fallback_used']}")
        if res["fallback_reason"]:
            print(f"    fallback_reason   : {res['fallback_reason']}")
        print(f"    sql               : {res['generated_sql']}")
        print(f"    parameters        : {res['query_parameters']}")
        print(f"    rows              : {res['rows'][:3]}")
        ok = res["status"] == "success" and res["generation_method"] == "gemini" and check(res["rows"])
        print(f"    {'PASS' if ok else 'FAIL'}  ({elapsed:.2f}s)")
        failures += 0 if ok else 1

    db.close()
    print(f"\nResult: {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

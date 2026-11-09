#!/usr/bin/env python3
"""
PL Capital AI Portfolio Analytics Agent — Command-Line Interface.

Usage:
    Interactive mode:   python main.py
    One-shot mode:      python main.py --question "How many portfolios do we have in total?"
    Pretty output:      python main.py --pretty --question "..."
    Custom DB path:     python main.py --db path/to/portfolio.db
"""

from __future__ import annotations

import argparse
import json
import sys
import os

# Ensure the project root is on sys.path so that `portfolio_agent` is importable
# regardless of the working directory the script is called from.
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from portfolio_agent.database import PortfolioDatabase
from portfolio_agent.agent import PortfolioAgent
from portfolio_agent.tools import SQLQueryTool


# ─── Formatting helpers ─────────────────────────────────────────────

def _format_pretty(response: dict) -> str:
    """
    Build a human-friendly multi-line string from a structured agent response.
    """
    lines: list[str] = []

    status = response.get("status", "unknown")
    tool = response.get("selected_tool", "N/A")
    lines.append(f"  Status : {status}")
    lines.append(f"  Tool   : {tool}")

    if response.get("route_reason"):
        lines.append(f"  Reason : {response['route_reason']}")

    result = response.get("result")
    if result is None:
        if response.get("error"):
            lines.append(f"\n  Error: {response['error']}")
        return "\n".join(lines)

    # ── SQL Query Tool result ──
    if tool == "sql_query":
        method = result.get("generation_method", "N/A")
        if result.get("model_name"):
            method = f"{method} ({result['model_name']})"
        lines.append(f"  Method : {method}")
        if result.get("fallback_used"):
            lines.append(f"  Fallback: {result.get('fallback_reason')}")
        sql = result.get("generated_sql")
        if sql:
            lines.append(f"\n  SQL:\n    {sql}")
        params = result.get("query_parameters")
        if params:
            lines.append(f"  Parameters: {params}")
        lines.append(f"  Rows returned: {result.get('row_count', 0)}")

        rows = result.get("rows", [])
        columns = result.get("columns", [])
        if rows and columns:
            lines.append("")
            lines.append(_format_table(columns, rows))

    # ── Exposure Calculator result ──
    elif tool == "exposure_calculator":
        pname = result.get("portfolio_name", "N/A")
        lines.append(f"\n  Portfolio: {pname}")
        lines.append(f"  Basis   : {result.get('exposure_basis', 'N/A')}")
        lines.append(f"  Filter  : {result.get('asset_filter', 'N/A')}")

        exposures = result.get("exposures", [])
        if exposures:
            lines.append("")
            exp_cols = ["Sector", "Exposure %", "Holdings"]
            exp_rows = [
                {
                    "Sector": e["sector"],
                    "Exposure %": f"{e['exposure_pct']:.2f}%",
                    "Holdings": str(e["holding_count"]),
                }
                for e in exposures
            ]
            lines.append(_format_table(exp_cols, exp_rows))

        total_pct = result.get("total_equity_weight_pct", 0)
        lines.append(f"  Total equity weight: {total_pct:.2f}%")
        lines.append(f"  Total holdings: {result.get('total_holdings_count', 0)}")
        lines.append(f"  Equity holdings: {result.get('equity_holdings_count', 0)}")
        lines.append(f"  Excluded bonds: {result.get('excluded_bond_holdings', 0)}")

    if status != "success" and response.get("error"):
        lines.append(f"\n  Error: {response['error']}")

    return "\n".join(lines)


def _format_table(columns: list[str], rows: list[dict]) -> str:
    """Build a simple ASCII table from column names and row dicts."""
    if not rows:
        return "  (no data)"

    # Compute column widths
    col_widths = {c: len(str(c)) for c in columns}
    for row in rows:
        for c in columns:
            val = str(row.get(c, ""))
            col_widths[c] = max(col_widths[c], len(val))

    # Header
    header = "  | " + " | ".join(str(c).ljust(col_widths[c]) for c in columns) + " |"
    sep = "  |-" + "-|-".join("-" * col_widths[c] for c in columns) + "-|"

    lines = [header, sep]
    for row in rows:
        line = "  | " + " | ".join(
            str(row.get(c, "")).ljust(col_widths[c]) for c in columns
        ) + " |"
        lines.append(line)

    return "\n".join(lines)


# ─── CLI entry points ───────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="PL Capital AI Portfolio Analytics Agent — CLI",
    )
    parser.add_argument(
        "--question", "-q",
        type=str,
        default=None,
        help="Run a single question in one-shot mode and exit.",
    )
    parser.add_argument(
        "--pretty", "-p",
        action="store_true",
        default=False,
        help="Format output for human readability instead of raw JSON.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to the SQLite database file (default: database/portfolio.db).",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        default=False,
        help="Disable Gemini LLM integration (use rule-based SQL generator only).",
    )
    return parser


def _check_database(db: PortfolioDatabase) -> bool:
    """Return True if the database is ready, else print guidance and return False."""
    if not db.db_path.is_file():
        print(
            f"\nError: Database not found at {db.db_path}\n"
            "Please initialize the database first:\n\n"
            "    python init_db.py\n",
            file=sys.stderr,
        )
        return False
    if not db.is_initialized():
        print(
            f"\nError: Database at {db.db_path} is not properly initialized.\n"
            "Please rebuild the database:\n\n"
            "    python init_db.py --reset\n",
            file=sys.stderr,
        )
        return False
    return True


def _print_response(response: dict, pretty: bool) -> None:
    """Print the agent response in the chosen format."""
    if pretty:
        print(_format_pretty(response))
    else:
        print(json.dumps(response, indent=2, default=str))


def run_interactive(agent: PortfolioAgent, pretty: bool) -> None:
    """Run the agent in interactive prompt-loop mode."""
    print("\n" + "=" * 60)
    print("  PL Capital AI Portfolio Analytics Agent")
    print("=" * 60)
    print("  Type your question and press Enter.")
    print("  Type 'exit', 'quit', or 'q' to leave.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            break

        if not user_input:
            continue

        if user_input.lower() in ("exit", "quit", "q"):
            print("Goodbye!")
            break

        response = agent.answer_question(user_input)
        print("\nAgent:")
        _print_response(response, pretty)
        print()


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on error."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Build database handle
    db = PortfolioDatabase(db_path=args.db)

    if not _check_database(db):
        return 1

    sql_tool = SQLQueryTool(db=db, use_gemini=not args.no_llm)
    agent = PortfolioAgent(db=db, sql_tool=sql_tool)

    if args.question:
        # One-shot mode
        response = agent.answer_question(args.question)
        _print_response(response, args.pretty)
        status = response.get("status", "error")
        return 0 if status in ("success", "unsupported", "no_equity_holdings") else 1
    else:
        # Interactive mode
        run_interactive(agent, args.pretty)
        return 0


def load_environment() -> None:
    """
    Load GEMINI_* settings from the project's .env file, if python-dotenv is installed.
    Existing environment variables take precedence (override=False).
    Skipped when PORTFOLIO_AGENT_SKIP_DOTENV=1 (used by the automated test suite so
    tests never make live API calls).
    """
    if os.environ.get("PORTFOLIO_AGENT_SKIP_DOTENV") == "1":
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(_SCRIPT_DIR, ".env"), override=False)


if __name__ == "__main__":
    load_environment()
    sys.exit(main())

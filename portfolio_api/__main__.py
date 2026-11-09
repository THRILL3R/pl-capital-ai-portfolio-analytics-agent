"""
Run the local web adapter:   python -m portfolio_api [--port 8000] [--no-llm]

Loads GEMINI_* settings from the project's .env exactly like ``main.py`` does (server-side
only; nothing from the environment is ever sent to the browser). Binds to loopback only
because the prototype has no authentication.
"""

from __future__ import annotations

import argparse
import sys

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m portfolio_api", description="Local web adapter for the PL Capital agent.")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (loopback only; default 127.0.0.1).")
    p.add_argument("--port", type=int, default=8000, help="Port (default 8000).")
    p.add_argument("--db", default=None, help="SQLite database path (default: database/portfolio.db).")
    p.add_argument("--no-llm", action="store_true", help="Disable Gemini; use the deterministic rule-based SQL generator only.")
    p.add_argument("--no-intent-classifier", action="store_true", help="Disable the local semantic intent classifier (legacy routing; for debugging).")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.host not in LOOPBACK:
        print("Refusing to bind to a non-loopback address: this prototype has no authentication.", file=sys.stderr)
        return 2

    import uvicorn

    import main as cli  # existing CLI module: reuse its .env loader
    from .app import create_app
    from .service import PortfolioService

    cli.load_environment()
    service = PortfolioService(db_path=args.db, use_llm=not args.no_llm, use_intent_classifier=False if args.no_intent_classifier else None)
    problem = service.agent.prepare_intent_classifier()  # loads the local model now; reports a setup problem early
    if problem:
        print(f"Note: {problem}", file=sys.stderr)
    if not service.health()["database_ready"]:
        print("Database not initialised. Run: python init_db.py", file=sys.stderr)
        return 1
    uvicorn.run(create_app(service), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
Database Initialization Script for PL Capital AI Portfolio Analytics Agent.

Usage:
    python init_db.py              # Initialize if missing/incomplete, or report existing state
    python init_db.py --reset      # Rebuild the database from original schema and CSVs
    python init_db.py --db custom.db --schema custom_schema.sql --data custom_data_dir/
"""

import argparse
import logging
import sys
from pathlib import Path

# Add project root to sys.path so portfolio_agent package is discoverable
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio_agent.database import (
    PortfolioDatabase,
    EXPECTED_TABLE_COUNTS,
    TOTAL_EXPECTED_RECORDS,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize or reset the PL Capital Portfolio SQLite database from CSV datasets."
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Rebuild the database from scratch using database_schema.sql and CSV datasets.",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database file (default: database/portfolio.db).",
    )
    parser.add_argument(
        "--schema",
        type=str,
        default=None,
        help="Path to database_schema.sql (default: database_schema.sql).",
    )
    parser.add_argument(
        "--data",
        type=str,
        default=None,
        help="Path to data directory containing CSV files (default: data/).",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity level.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    db_path = PortfolioDatabase.resolve_database_path(args.db)
    schema_path = PortfolioDatabase.resolve_schema_path(args.schema)
    data_dir = PortfolioDatabase.resolve_data_dir(args.data)

    print("=" * 70)
    print("PL CAPITAL - AI PORTFOLIO ANALYTICS AGENT: DATABASE INITIALIZER")
    print("=" * 70)
    print(f"Target Database : {db_path}")
    print(f"Schema File     : {schema_path}")
    print(f"Data Directory  : {data_dir}")
    print(f"Action Mode     : {'RESET (rebuild from scratch)' if args.reset else 'NORMAL (initialize or verify)'}")
    print("-" * 70)

    try:
        db = PortfolioDatabase(db_path)
        result = db.initialize_database(
            schema_path=schema_path,
            data_dir=data_dir,
            reset=args.reset,
        )

        status = result.get("status")
        print("\nInitialization Result:")

        if status == "already_initialized":
            print("  Status: DATABASE ALREADY INITIALIZED (no action required, zero duplication)")
            integrity = result.get("integrity", {})
            table_counts = integrity.get("table_counts", {})
            total_records = integrity.get("total_records", 0)
        else:
            print("  Status: DATABASE SUCCESSFULLY BUILT")
            table_counts = result.get("table_counts", {})
            total_records = result.get("total_records", 0)

        print("\nTable Record Counts:")
        for tbl, expected in EXPECTED_TABLE_COUNTS.items():
            actual = table_counts.get(tbl, 0)
            status_indicator = "OK" if actual == expected else "MISMATCH"
            print(f"  - {tbl:<24}: {actual:>5} rows (expected: {expected:>5}) [{status_indicator}]")

        print("-" * 70)
        print(f"Total Database Records: {total_records} / {TOTAL_EXPECTED_RECORDS} expected")

        # Run integrity verification
        verification = db.verify_integrity()
        fk_violations = verification.get("fk_violations_count", 0)
        integrity_status = verification.get("integrity_check", "unknown")

        print(f"PRAGMA foreign_key_check: {fk_violations} violations")
        print(f"PRAGMA integrity_check  : {integrity_status}")

        if not verification.get("is_valid", False):
            print("\n[ERROR] Integrity verification failed! Please check dataset or schema.")
            return 1

        print("\nDatabase is fully verified, intact, and ready for queries.")
        print("=" * 70)
        db.close()
        return 0

    except Exception as exc:
        print(f"\n[FATAL ERROR] Database initialization failed: {exc}")
        logging.exception("Database initialization failure")
        return 1


if __name__ == "__main__":
    sys.exit(main())

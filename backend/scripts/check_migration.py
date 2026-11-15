"""Verification helper: inspect the schema produced by Alembic.

Run with:  uv run python scripts/check_migration.py [db_path]

This exists because "the migration ran without error" is not the same claim as "the migration
produced the documented schema". It compares the physical schema against the expected table list
from docs/DATABASE_DESIGN.md and reports foreign keys and indexes.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

EXPECTED = {
    "users",
    "audit_logs",
    "proxy_sources",
    "proxies",
    "proxy_checks",
    "email_domains",
    "inboxes",
    "messages",
    "registration_targets",
    "campaigns",
    "registration_tests",
    "registration_results",
    "background_jobs",
    "job_logs",
}

#: Alembic's own bookkeeping table. It is expected and is not part of the NEXUS schema.
TOOL_TABLES = {"alembic_version"}


def main(argv: list[str]) -> int:
    db_path = Path(argv[1] if len(argv) > 1 else "_migration_check.db")
    if not db_path.exists():
        print(f"FAIL: database {db_path} does not exist")
        return 1

    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        fk_count = sum(
            len(list(connection.execute(f"PRAGMA foreign_key_list({table})"))) for table in tables
        )
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    finally:
        connection.close()

    print(f"database:        {db_path}")
    print(f"alembic version: {revision}")
    print(f"tables found:    {len(tables)}")
    print(f"indexes found:   {len(indexes)}")
    print(f"foreign keys:    {fk_count}")

    missing = EXPECTED - tables
    extra = tables - EXPECTED - TOOL_TABLES
    if missing:
        print(f"FAIL: missing tables: {sorted(missing)}")
    if extra:
        print(f"FAIL: unexpected tables: {sorted(extra)}")

    if missing or extra:
        return 1

    print("PASS: every documented table exists and nothing unexpected was created")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Initialize the application database and manage explicit SQLite transactions."""

import argparse
import json
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from tool_agent_lab.settings import load_settings

SCHEMA_VERSION = 2
INITIAL_MIGRATION = Path(__file__).with_name("migrations") / "001_initial.sql"
EXECUTION_MIGRATION = Path(__file__).with_name("migrations") / "002_execution.sql"


def require_transaction(connection: sqlite3.Connection) -> None:
    """Prevent repository writes from silently using SQLite autocommit."""
    if not connection.in_transaction:
        raise RuntimeError("Repository writes require an explicit transaction")


@contextmanager
def connect(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open a foreign-key-enabled connection; close it on exit without implicit commits."""
    connection = sqlite3.connect(database_path, isolation_level=None, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        yield connection
    finally:
        connection.close()


@contextmanager
def transaction(database_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Commit all writes together, or roll them back if the block or commit fails."""
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def initialize_database(database_path: str | Path) -> int:
    """Apply pending migrations atomically; repeated initialization preserves records."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with transaction(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return version
        if version < 0 or version > SCHEMA_VERSION:
            raise ValueError(f"Unsupported database schema version: {version}")

        # executescript would commit the active transaction before running the SQL.
        for target, migration in ((1, INITIAL_MIGRATION), (2, EXECUTION_MIGRATION)):
            if target <= version:
                continue
            statement = ""
            for line in migration.read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    connection.execute(statement)
                    statement = ""
            if statement.strip():
                raise ValueError(f"Incomplete SQL statement in {migration.name}")
            connection.execute(f"PRAGMA user_version = {target}")
    return SCHEMA_VERSION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="Database path; default is configured app_db_path")
    args = parser.parse_args(argv)
    path = args.database.resolve() if args.database is not None else load_settings().app_db_path
    version = initialize_database(path)
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"database": str(path), "schema_version": version}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

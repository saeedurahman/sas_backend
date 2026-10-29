"""Create the test database named by TEST_DATABASE_URL and migrate it to head.

    python scripts/create_test_db.py

Safe to re-run: an existing database is kept and only migrated. Refuses to run when
TEST_DATABASE_URL is unset or points at DATABASE_URL's database. The role in the URL needs
CREATEDB (or create the empty database yourself first; the migration step still runs).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _ensure_database(url) -> None:
    import psycopg2
    from psycopg2 import sql

    # Connect to the server's maintenance database; the target may not exist yet.
    conn = psycopg2.connect(
        dbname="postgres",
        user=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (url.database,))
            if cur.fetchone():
                print(f"Database {url.database!r} already exists.")
                return
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(url.database)))
            print(f"Created database {url.database!r}.")
    finally:
        conn.close()


def main() -> int:
    from tests.helpers.testdb import TestDatabaseConfigError, require_test_database_url, sync_url

    try:
        test_url = require_test_database_url()
    except TestDatabaseConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    url = sync_url(test_url)
    if not url.database:
        print("Error: TEST_DATABASE_URL has no database name", file=sys.stderr)
        return 1
    print(f"Target: {url.host or 'localhost'}:{url.port or 5432}/{url.database}")

    try:
        _ensure_database(url)
    except Exception as exc:  # noqa: BLE001 - surface any connection/permission error plainly
        print(f"Error creating database: {exc}", file=sys.stderr)
        return 1

    # alembic/env.py reads DATABASE_URL from settings; point it at the test database.
    env = {**os.environ, "DATABASE_URL": test_url}
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env)
    if result.returncode != 0:
        print("Error: alembic upgrade failed", file=sys.stderr)
        return result.returncode
    print("Test database is migrated to head.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

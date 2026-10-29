"""Resolve and validate the database the test suite is allowed to touch.

Tests create throw-away tenants, so they must never run against the development
database. ``TEST_DATABASE_URL`` (environment or ``.env``) names a separate database;
it must be set and must not point at the same database as ``DATABASE_URL``.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[2]

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


class TestDatabaseConfigError(RuntimeError):
    __test__ = False  # not a pytest class


def _setting(name: str) -> str | None:
    """Environment first, then ``.env`` (same precedence the app's settings use)."""
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(ROOT / ".env").get(name)
    value = (value or "").strip()
    return value or None


def _identity(url: str) -> tuple[str, int, str]:
    parsed = make_url(url)
    host = (parsed.host or "").lower()
    host = "localhost" if host in _LOCAL_HOSTS else host
    return host, parsed.port or 5432, parsed.database or ""


def sync_url(url: str) -> URL:
    """``postgresql[+driver]://`` -> psycopg2 URL for sync helpers and alembic."""
    return make_url(url).set(drivername="postgresql+psycopg2")


def development_database_url() -> str | None:
    return _setting("DATABASE_URL")


def configured_test_database_url() -> str | None:
    return _setting("TEST_DATABASE_URL")


@cache
def require_test_database_url() -> str:
    """Return ``TEST_DATABASE_URL`` or raise if it is unset or is the dev database.

    Cached: conftest later overwrites ``DATABASE_URL`` with the test URL, so the
    comparison against the development URL must happen exactly once, before that.
    """
    test_url = configured_test_database_url()
    if test_url is None:
        raise TestDatabaseConfigError(
            "TEST_DATABASE_URL is not set. The tests create throw-away data and refuse to "
            "run without a dedicated database. Create one with "
            "'python scripts/create_test_db.py' and set TEST_DATABASE_URL (see .env.example)."
        )
    dev_url = development_database_url()
    if dev_url is not None and _identity(test_url) == _identity(dev_url):
        raise TestDatabaseConfigError(
            "TEST_DATABASE_URL points at the same database as DATABASE_URL "
            f"({_identity(test_url)[2]!r}). Refusing to run tests against it."
        )
    return test_url

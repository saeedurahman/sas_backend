"""Sync SQL helpers for post-condition verification in integration tests."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.core.config import settings


def sync_db_url() -> str:
    url = settings.database_url
    if url.startswith("postgresql+asyncpg://"):
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    return url


@lru_cache(maxsize=1)
def _engine() -> Engine:
    return create_engine(sync_db_url(), pool_pre_ping=True)


def db_scalar(sql: str, params: dict[str, Any] | None = None) -> Any:
    with _engine().connect() as conn:
        return conn.execute(text(sql), params or {}).scalar()


def db_rows(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with _engine().connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params or {}).mappings().all()]


def db_execute(sql: str, params: dict[str, Any] | None = None) -> None:
    with _engine().begin() as conn:
        conn.execute(text(sql), params or {})

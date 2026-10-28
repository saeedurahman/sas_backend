"""Synchronous direct-DB helper for arranging/inspecting state in tests."""

from __future__ import annotations

from sqlalchemy import create_engine, text

from app.core.config import settings

_engine = None


def _sync_url() -> str:
    url = settings.database_url
    for prefix in ("postgresql+asyncpg://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg2://" + url[len(prefix):]
    return url


def sql(statement: str, **params):
    """Run one statement in its own transaction; return all rows (or [])."""
    global _engine
    if _engine is None:
        _engine = create_engine(_sync_url(), pool_pre_ping=True)
    with _engine.begin() as conn:
        result = conn.execute(text(statement), params)
        return result.fetchall() if result.returns_rows else []

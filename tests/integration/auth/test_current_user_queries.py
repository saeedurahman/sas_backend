"""Carry-over D: get_current_user no longer drags in every tenant's role_permissions."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core import permission_cache
from app.core.config import settings
from app.dependencies import _user_permission_keys, get_current_user

pytestmark = pytest.mark.integration


def _run(token: str) -> tuple[list[str], list[str], set[str], set[str]]:
    async def go():
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        statements: list[str] = []

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, params, context, executemany):
            statements.append(statement)

        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            async with maker() as db:
                user = await get_current_user(token, db)
                cold_keys = _user_permission_keys(user)
            cold = list(statements)
            statements.clear()
            async with maker() as db:
                user = await get_current_user(token, db)
                warm_keys = _user_permission_keys(user)
            return cold, list(statements), cold_keys, warm_keys
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_no_cross_tenant_role_permission_load_and_cache_skips_queries(registered_owner):
    permission_cache.invalidate_all()
    token = registered_owner.owner_headers["Authorization"].split(" ", 1)[1]
    cold, warm, cold_keys, warm_keys = _run(token)

    assert cold_keys and cold_keys == warm_keys

    # The permission -> role_permissions back-reference used to be eager-loaded,
    # i.e. "FROM role_permissions WHERE permission_id IN (...)" across all tenants.
    for stmt in cold + warm:
        if "FROM role_permissions" in stmt:
            assert "role_permissions.permission_id IN" not in stmt
    # users with this role are not loaded back either
    assert not any("FROM user_roles" in s and "user_roles.role_id IN" in s for s in cold)

    cold_rp = sum("FROM role_permissions" in s for s in cold)
    warm_rp = sum("FROM role_permissions" in s for s in warm)
    assert cold_rp == 1 and warm_rp == 0
    assert len(warm) < len(cold)


def test_permission_cache_is_dropped_when_roles_change(http_client, rbac_tenant):
    t = rbac_tenant
    # warm the cache: the cashier can list sales
    assert http_client.get(t.url("/sales"), headers=t.cashier_headers).status_code == 200
    roles = http_client.get(t.url("/roles"), headers=t.owner_headers).json()
    cashier = next(r for r in roles if r["name"].lower() == "cashier")
    keep = [k for k in cashier["permission_keys"] if k != "sales.view"]
    put = http_client.put(
        t.url(f"/roles/{cashier['id']}/permissions"),
        headers=t.owner_headers,
        json={"permission_keys": keep},
    )
    assert put.status_code == 200, put.text
    # no waiting for the TTL: the edit invalidates the cache
    assert http_client.get(t.url("/sales"), headers=t.cashier_headers).status_code == 403

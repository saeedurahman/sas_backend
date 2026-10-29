"""Crash safety: an op's effects, stored result and audit row commit together or not at all."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.models.business import Business
from app.models.user import Role, RolePermission, User, UserRole
from app.schemas.sync import PushRequest
from app.services import sync_push
from tests.helpers.db import db_scalar
from tests.helpers.phase2 import D, stock_qty
from tests.helpers.sync import op, sale_payload

pytestmark = pytest.mark.integration


class SimulatedCrash(BaseException):
    """BaseException: nothing in the app catches it, like a killed process."""


async def _push(user_id: str, request: PushRequest, *, crash_in: str | None = None, monkeypatch=None):
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as db:
            user = (
                await db.execute(
                    select(User)
                    .where(User.id == uuid.UUID(user_id))
                    .options(
                        selectinload(User.business).options(
                            selectinload(Business.business_type), selectinload(Business.config)
                        ),
                        selectinload(User.default_branch),
                        selectinload(User.user_roles)
                        .selectinload(UserRole.role)
                        .selectinload(Role.role_permissions)
                        .selectinload(RolePermission.permission),
                    )
                )
            ).scalar_one()
            if crash_in:
                real = getattr(sync_push, crash_in)

                async def boom(*args, **kwargs):
                    await real(*args, **kwargs)  # do the work, then die before the commit
                    raise SimulatedCrash()

                monkeypatch.setattr(sync_push, crash_in, boom)
            return await sync_push.process_push(db, user, request)
    finally:
        await engine.dispose()


def _owner_id(http_client, t) -> str:
    return http_client.get(t.url("/auth/me"), headers=t.owner_headers).json()["id"]


@pytest.mark.parametrize("crash_in", ["_store", "log_audit"])
def test_crash_before_commit_leaves_nothing_and_retry_applies_once(
    http_client, pos_tenant, monkeypatch, crash_in
):
    t = pos_tenant
    owner = _owner_id(http_client, t)
    sale = op("sale.create", sale_payload(t, qty="5"))
    request = PushRequest(device_id="dev-crash", batch_id="b1", ops=[sale])
    before = stock_qty(t)

    with monkeypatch.context() as patch:
        with pytest.raises(SimulatedCrash):
            asyncio.run(_push(owner, request, crash_in=crash_in, monkeypatch=patch))

    # Nothing from the half-finished op survived: no sale, stock, result row, audit or numbering.
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 0
    assert db_scalar("SELECT count(*) FROM sync_ops WHERE business_id = :b", {"b": t.business_id}) == 0
    assert db_scalar("SELECT count(*) FROM audit_logs WHERE business_id = :b AND action = 'sync'",
                     {"b": t.business_id}) == 0
    assert db_scalar("SELECT count(*) FROM register_transactions WHERE business_id = :b AND tx_type = 'sale'",
                     {"b": t.business_id}) == 0
    assert stock_qty(t) == before
    assert D(db_scalar("SELECT COALESCE(SUM(qty),0) FROM stock_levels WHERE business_id = :b AND product_id = :p",
                       {"b": t.business_id, "p": t.product_id})) == before

    # The device retries the same batch: applied exactly once.
    results = asyncio.run(_push(owner, request))
    assert [r["status"] for r in results] == ["applied"]
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 1
    assert stock_qty(t) == before - 5

    # ...and once more (response lost again): a duplicate, no second effect.
    again = asyncio.run(_push(owner, request))
    assert [r["status"] for r in again] == ["duplicate"]
    assert again[0]["server_id"] == results[0]["server_id"]
    assert stock_qty(t) == before - 5


def test_crash_between_ops_keeps_finished_ops_and_retry_finishes_the_rest(
    http_client, pos_tenant, monkeypatch
):
    t = pos_tenant
    owner = _owner_id(http_client, t)
    ops = [op("sale.create", sale_payload(t, qty="1")) for _ in range(3)]
    request = PushRequest(device_id="dev-crash", batch_id="b2", ops=ops)

    real = sync_push._run_op
    calls = {"n": 0}

    async def crash_on_third(ctx, envelope):
        calls["n"] += 1
        if calls["n"] == 3:
            raise SimulatedCrash()
        return await real(ctx, envelope)

    with monkeypatch.context() as patch:
        patch.setattr(sync_push, "_run_op", crash_on_third)
        with pytest.raises(SimulatedCrash):
            asyncio.run(_push(owner, request))

    # Ops 1 and 2 committed on their own; op 3 never started.
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 2
    results = asyncio.run(_push(owner, request))
    assert [r["status"] for r in results] == ["duplicate", "duplicate", "applied"]
    assert db_scalar("SELECT count(*) FROM sales WHERE business_id = :b", {"b": t.business_id}) == 3
    assert stock_qty(t) == D("97")

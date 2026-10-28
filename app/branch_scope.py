"""Branch isolation for branch-assigned staff.

Owners (and business-wide staff) see every branch. A user with an assigned branch, or
with roles scoped through ``user_roles.branch_id``, may only touch those branches; any
other ``branch_id`` gets 403.

Pieces, all FastAPI dependencies:

* ``scoped_branch_query`` / ``required_branch_query`` - replace the ``branch_id`` query
  parameter of list/report endpoints. A restricted user who omits it is pinned to their
  own branch (or must choose one when they have several).
* ``guard_body_branches`` - rejects a foreign ``branch_id`` / ``source_branch_id`` in the
  JSON body of create/update calls.
* ``guard_record`` - loads the branch of a record addressed by path id (sale, shift, ...)
  and rejects foreign ones.
"""

from __future__ import annotations

from collections.abc import Callable
from uuid import UUID

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import allowed_branch_ids, get_current_user
from app.models.user import User

FOREIGN_BRANCH = "You do not have access to this branch"

# table -> columns holding a branch; access to ANY listed branch is enough to view/act
# (a transfer is visible to both ends; creating one is checked on the source in the body).
_RECORD_TABLES: dict[str, tuple[str, ...]] = {
    "sales": ("branch_id",),
    "sale_returns": ("branch_id",),
    "register_shifts": ("branch_id",),
    "cash_registers": ("branch_id",),
    "stock_transfers": ("source_branch_id", "dest_branch_id"),
    "expenses": ("branch_id",),
    "purchase_orders": ("branch_id",),
    "purchase_receipts": ("branch_id",),
    "stock_adjustments": ("branch_id",),
    "waste_entries": ("branch_id",),
}

_BODY_BRANCH_KEYS = ("branch_id", "source_branch_id")


def assert_branch_access(user: User, branch_id: UUID | None) -> None:
    if branch_id is None:
        return
    allowed = allowed_branch_ids(user)
    if allowed is not None and branch_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FOREIGN_BRANCH)


def effective_branch(user: User, branch_id: UUID | None) -> UUID | None:
    """Validate ``branch_id``; pin a restricted user who sent none to their branch."""
    allowed = allowed_branch_ids(user)
    if allowed is None:
        return branch_id
    if branch_id is not None:
        assert_branch_access(user, branch_id)
        return branch_id
    if len(allowed) == 1:
        return next(iter(allowed))
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="branch_id is required: you have access to more than one branch",
    )


async def scoped_branch_query(
    branch_id: UUID | None = Query(default=None),
    current_user: User = Depends(get_current_user),
) -> UUID | None:
    return effective_branch(current_user, branch_id)


async def required_branch_query(
    branch_id: UUID = Query(),
    current_user: User = Depends(get_current_user),
) -> UUID:
    assert_branch_access(current_user, branch_id)
    return branch_id


async def guard_body_branches(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> None:
    if request.method not in {"POST", "PUT", "PATCH"}:
        return
    if allowed_branch_ids(current_user) is None:
        return
    try:
        body = await request.json()
    except Exception:
        return  # not JSON: normal validation reports it
    if not isinstance(body, dict):
        return
    for key in _BODY_BRANCH_KEYS:
        raw = body.get(key)
        if raw is None:
            continue
        try:
            assert_branch_access(current_user, UUID(str(raw)))
        except ValueError:
            continue  # malformed uuid: normal validation reports it


def guard_record(table: str, path_param: str) -> Callable[..., object]:
    """Dependency factory: the record at ``{path_param}`` must sit in an allowed branch."""
    columns = _RECORD_TABLES[table]  # KeyError at import time = typo, not a runtime hole
    select_sql = text(
        f"SELECT {', '.join(columns)} FROM {table} "  # noqa: S608 - table/columns are constants
        "WHERE id = :id AND business_id = :business_id"
    )

    async def _guard(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        allowed = allowed_branch_ids(current_user)
        if allowed is None:
            return
        try:
            record_id = UUID(str(request.path_params[path_param]))
        except (KeyError, ValueError):
            return
        row = (
            await db.execute(
                select_sql, {"id": record_id, "business_id": current_user.business_id}
            )
        ).first()
        if row is None:
            return  # the endpoint answers 404
        branches = [b for b in row if b is not None]
        if branches and not any(b in allowed for b in branches):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FOREIGN_BRANCH)

    return _guard


def guard_body_record(body_key: str, table: str) -> Callable[..., object]:
    """Dependency factory: the record referenced by ``body[body_key]`` must be in-scope."""
    columns = _RECORD_TABLES[table]
    select_sql = text(
        f"SELECT {', '.join(columns)} FROM {table} "  # noqa: S608 - constants only
        "WHERE id = :id AND business_id = :business_id"
    )

    async def _guard(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> None:
        allowed = allowed_branch_ids(current_user)
        if allowed is None:
            return
        try:
            body = await request.json()
            record_id = UUID(str(body[body_key]))
        except Exception:
            return
        row = (
            await db.execute(
                select_sql, {"id": record_id, "business_id": current_user.business_id}
            )
        ).first()
        if row is None:
            return
        branches = [b for b in row if b is not None]
        if branches and not any(b in allowed for b in branches):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FOREIGN_BRANCH)

    return _guard

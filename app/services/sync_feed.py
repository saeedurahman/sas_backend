"""Change feed for offline devices: opaque cursors, pull and paged snapshot.

Every synced row carries a per-business ``change_seq`` stamped at COMMIT (see migration
019), so sequence order is commit order and a cursor can never skip a late-committing row.
A cursor is the last delivered ``change_seq``, signed so it cannot be forged, together with
the server time it was issued (never a device clock) and the user + permissions fingerprint
it was issued for: a different fingerprint means the user's visible data may have changed
in ways the feed cannot replay, so the device must take a snapshot.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.branch_scope import assert_branch_access
from app.core.config import settings
from app.dependencies import (
    _user_permission_keys,
    allowed_branch_ids,
    permissions_version,
    user_roles_for_response,
)
from app.models.user import Role, RolePermission, User, UserRole
from app.services import subscription as sub

DEFAULT_LIMIT = 500
MAX_LIMIT = 1000

REASON_EXPIRED = "cursor_expired"
REASON_PERMISSIONS = "permissions_changed"
REASON_USER = "user_changed"
REASON_AHEAD = "cursor_ahead_of_server"


class CursorError(Exception):
    pass


# ── cursors ──────────────────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text_: str) -> bytes:
    return base64.urlsafe_b64decode(text_ + "=" * (-len(text_) % 4))


def _sign(body: bytes) -> str:
    key = settings.jwt_secret_key.encode()
    return _b64(hmac.new(key, b"sync-cursor:" + body, hashlib.sha256).digest()[:18])


def encode_token(payload: dict[str, Any]) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return f"{_b64(body)}.{_sign(body)}"


def decode_token(token: str) -> dict[str, Any]:
    try:
        body_b64, signature = token.split(".", 1)
        body = _unb64(body_b64)
        if not hmac.compare_digest(_sign(body), signature):
            raise CursorError("bad signature")
        payload = json.loads(body)
    except CursorError:
        raise
    except Exception as exc:  # noqa: BLE001 - any malformed token is just "invalid"
        raise CursorError("malformed") from exc
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise CursorError("unsupported version")
    return payload


def invalid_cursor() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"code": "invalid_cursor", "message": "The cursor is not valid; start with a snapshot."},
    )


def _cursor_for(user: User, seq: int, pv: str, now: datetime) -> str:
    return encode_token(
        {
            "v": 1,
            "k": "pull",
            "b": str(user.business_id),
            "u": str(user.id),
            "s": seq,
            "t": int(now.timestamp()),
            "p": pv,
        }
    )


# ── entities ─────────────────────────────────────────────────────────────────

_BOOKKEEPING = [
    "business_id",
    "change_seq",
    "sync_status",
    "local_id",
    "server_id",
    "created_by",
    "updated_by",
    "deleted_by",
]

_CATALOG_PERMS = ("products.view", "sales.create", "sales.view", "inventory.view")


@dataclass(frozen=True)
class Entity:
    name: str
    table: str
    perms: tuple[str, ...] | None  # any one of these; None = every signed-in user
    branch_col: str | None = None  # column limited to the caller's branches
    exclude: tuple[str, ...] = ()
    builder: str | None = None  # custom materialisation


# Snapshot order: reference data first, so a half-applied snapshot is still self-consistent.
ENTITIES: tuple[Entity, ...] = (
    Entity("settings", "business_configs", None, builder="settings"),
    Entity("branch", "branches", None, branch_col="id"),
    Entity("cash_register", "cash_registers", ("registers.view", "shifts.open", "sales.create"),
           branch_col="branch_id"),
    Entity("role", "roles", ("roles.view", "sales.create", "shifts.open"), builder="role"),
    Entity("user", "users", ("users.view", "sales.create", "shifts.open"), builder="user"),
    Entity("category", "categories", _CATALOG_PERMS),
    Entity("brand", "brands", _CATALOG_PERMS),
    Entity("unit", "units", _CATALOG_PERMS),
    Entity("tax_rate", "tax_rates", _CATALOG_PERMS),
    Entity("product", "products", _CATALOG_PERMS),
    Entity("product_variation", "product_variations", _CATALOG_PERMS),
    Entity("barcode", "barcodes", _CATALOG_PERMS),
    Entity("price_list", "price_lists", _CATALOG_PERMS),
    Entity("price_list_item", "price_list_items", _CATALOG_PERMS),
    Entity("customer", "customers", ("customers.view", "sales.create"), builder="customer"),
    Entity("discount_scheme", "discount_schemes", ("discounts.view", "sales.create")),
    Entity("stock_level", "stock_levels", ("inventory.view", "sales.create"),
           branch_col="branch_id", exclude=("variation_key",)),
)
ENTITY_BY_NAME = {e.name: e for e in ENTITIES}

# Served to a business whose subscription blocks everything: just what the expired screen needs.
RESTRICTED_ENTITIES = ("settings",)

# Holding none of these means the caller has no business syncing POS data at all.
PULL_PERMISSIONS = tuple(
    sorted({p for e in ENTITIES if e.perms for p in e.perms} | {"shifts.open", "shifts.close"})
)


def visible_entities(user: User, *, restricted: bool = False) -> list[Entity]:
    keys = _user_permission_keys(user)
    out = []
    for entity in ENTITIES:
        if restricted and entity.name not in RESTRICTED_ENTITIES:
            continue
        if entity.perms is None or keys.intersection(entity.perms):
            out.append(entity)
    return out


# ── row access ───────────────────────────────────────────────────────────────


@dataclass
class Scope:
    business_id: UUID
    user_id: UUID
    branches: list[str] | None  # None = unrestricted


def build_scope(user: User, branch_id: UUID | None) -> Scope:
    allowed = allowed_branch_ids(user)
    if branch_id is not None:
        assert_branch_access(user, branch_id)
        branches: list[str] | None = [str(branch_id)]
    elif allowed is not None:
        branches = sorted(str(b) for b in allowed)
    else:
        branches = None
    return Scope(user.business_id, user.id, branches)


def _loads(raw: str | None) -> dict[str, Any] | None:
    if raw is None:
        return None
    # Keep numerics exactly as Postgres printed them ("10.50"), as strings, like the REST API.
    return json.loads(raw, parse_float=str)


async def _select_rows(
    db: AsyncSession,
    entity: Entity,
    scope: Scope,
    *,
    since: int | None = None,
    after_id: UUID | None = None,
    limit: int,
    snapshot: bool = False,
) -> list[dict[str, Any]]:
    where = ["t.business_id = :business_id"]
    params: dict[str, Any] = {"business_id": scope.business_id, "limit": limit}
    if snapshot:
        where.append("t.deleted_at IS NULL")
        if after_id is not None:
            where.append("t.id > :after_id")
            params["after_id"] = after_id
        order = "t.id"
    else:
        where.append("t.change_seq > :since")
        params["since"] = since
        order = "t.change_seq"
    if scope.branches is not None:
        if entity.branch_col:
            where.append(f"t.{entity.branch_col} = ANY(CAST(:branches AS uuid[]))")
            params["branches"] = scope.branches
        elif entity.name == "user":
            where.append(
                "(t.default_branch_id IS NULL OR t.default_branch_id = ANY(CAST(:branches AS uuid[])) "
                "OR t.id = :self_id)"
            )
            params["branches"] = scope.branches
            params["self_id"] = scope.user_id
    exclude = [*_BOOKKEEPING, *entity.exclude]
    sql = (
        f"SELECT t.id, t.change_seq AS seq, (t.deleted_at IS NOT NULL) AS is_deleted, "
        f"(to_jsonb(t) - CAST(:exclude AS text[]))::text AS data FROM {entity.table} t "  # noqa: S608
        f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :limit"
    )
    params["exclude"] = exclude
    rows = (await db.execute(text(sql), params)).mappings().all()
    return [
        {"id": r["id"], "seq": r["seq"], "is_deleted": r["is_deleted"], "data": _loads(r["data"])}
        for r in rows
    ]


async def _materialise(
    db: AsyncSession, user: User, entity: Entity, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Replace raw row JSON with the entity's wire shape (users, roles, settings, balances)."""
    live = [r for r in rows if not r["is_deleted"]]
    if not live or entity.builder is None:
        return rows
    ids = [r["id"] for r in live]
    built: dict[UUID, dict[str, Any]] = {}

    if entity.builder == "customer":
        balances = (
            await db.execute(
                text(
                    "SELECT customer_id, COALESCE(SUM(amount), 0) AS balance FROM customer_ledger "
                    "WHERE business_id = :b AND customer_id = ANY(CAST(:ids AS uuid[])) "
                    "AND deleted_at IS NULL GROUP BY customer_id"
                ),
                {"b": user.business_id, "ids": [str(i) for i in ids]},
            )
        ).all()
        by_customer = {row.customer_id: Decimal(str(row.balance)) for row in balances}
        for row in live:
            row["data"]["balance"] = str(by_customer.get(row["id"], Decimal("0")).quantize(Decimal("0.01")))
        return rows

    if entity.builder == "settings":
        for row in live:
            data = row["data"]
            business = user.business
            state = sub.compute_state(business)
            contact = sub.contact_from_settings(await sub.load_platform_settings(db))
            built[row["id"]] = {
                "business_name": business.name,
                "currency_code": business.currency_code,
                "timezone": business.timezone,
                "default_tax_inclusive": data.get("default_tax_inclusive"),
                "allow_negative_stock": data.get("allow_negative_stock"),
                "fifo_costing_enabled": data.get("fifo_costing_enabled"),
                "receipt_prefix": data.get("receipt_prefix"),
                "invoice_prefix": data.get("invoice_prefix"),
                "enable_offline_mode": data.get("enable_offline_mode"),
                "features": data.get("config_json") or {},
                "subscription": {
                    "status": state.status,
                    "days_left": state.days_left,
                    "contact": contact,
                },
            }
    elif entity.builder == "role":
        roles = (
            await db.execute(
                select(Role)
                .where(Role.id.in_(ids), Role.business_id == user.business_id)
                .options(selectinload(Role.role_permissions).selectinload(RolePermission.permission))
                # The request's own roles were loaded with permissions skipped (cache hit).
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for role in roles:
            keys = sorted(
                rp.permission.permission_key
                for rp in role.role_permissions
                if rp.business_id == user.business_id and rp.permission is not None
            )
            built[role.id] = {
                "id": str(role.id),
                "name": role.name,
                "description": role.description,
                "is_system": role.is_system,
                "permission_keys": keys,
            }
    elif entity.builder == "user":
        users = (
            await db.execute(
                select(User)
                .where(User.id.in_(ids), User.business_id == user.business_id)
                .options(
                    selectinload(User.user_roles)
                    .selectinload(UserRole.role)
                    .selectinload(Role.role_permissions)
                    .selectinload(RolePermission.permission)
                )
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        for member in users:
            branches = allowed_branch_ids(member)
            built[member.id] = {
                "id": str(member.id),
                "name": member.full_name,
                "roles": user_roles_for_response(member),
                "role_ids": sorted(
                    str(ur.role_id)
                    for ur in member.user_roles
                    if ur.deleted_at is None and ur.role is not None and ur.role.deleted_at is None
                ),
                "branch_ids": None if branches is None else sorted(str(b) for b in branches),
                "status": member.status,
                "is_locked": member.is_locked,
                "permissions_version": permissions_version(member),
            }

    for row in rows:
        if not row["is_deleted"]:
            row["data"] = built.get(row["id"], row["data"])
    return rows


def _change(entity: Entity, row: dict[str, Any]) -> dict[str, Any]:
    deleted = row["is_deleted"]
    return {
        "entity": entity.name,
        "id": row["id"],
        "op": "delete" if deleted else "upsert",
        "data": None if deleted else row["data"],
        "seq": row["seq"],
    }


async def current_seq(db: AsyncSession, business_id: UUID) -> int:
    value = (
        await db.execute(
            text("SELECT last_seq FROM sync_counters WHERE business_id = :b"), {"b": business_id}
        )
    ).scalar_one_or_none()
    return int(value or 0)


def _clamp(limit: int | None) -> int:
    return max(1, min(limit or DEFAULT_LIMIT, MAX_LIMIT))


def _restricted(user: User) -> bool:
    return sub.compute_state(user.business).blocked_code is not None


# ── pull ─────────────────────────────────────────────────────────────────────


async def pull(
    db: AsyncSession,
    user: User,
    *,
    since: str | None,
    limit: int | None,
    branch_id: UUID | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    limit = _clamp(limit)
    pv = permissions_version(user)
    restricted = _restricted(user)
    entities = visible_entities(user, restricted=restricted)
    scope = build_scope(user, branch_id)
    head = await current_seq(db, user.business_id)

    since_seq = 0
    resync_reason: str | None = None
    if since:
        try:
            token = decode_token(since)
        except CursorError:
            raise invalid_cursor() from None
        if token.get("k") != "pull" or token.get("b") != str(user.business_id):
            raise invalid_cursor()
        since_seq = int(token.get("s", 0))
        issued = datetime.fromtimestamp(int(token.get("t", 0)), tz=timezone.utc)
        if now - issued > timedelta(days=settings.tombstone_retention_days):
            resync_reason = REASON_EXPIRED
        elif token.get("u") != str(user.id):
            resync_reason = REASON_USER
        elif token.get("p") != pv:
            resync_reason = REASON_PERMISSIONS
        elif since_seq > head:
            resync_reason = REASON_AHEAD

    base = {
        "server_time": now,
        "permissions_version": pv,
        "entities": [e.name for e in entities],
        "restricted": restricted,
    }
    if resync_reason:
        return {
            **base,
            "changes": [],
            "next_cursor": None,
            "has_more": False,
            "full_resync_required": True,
            "full_resync_reason": resync_reason,
        }

    if restricted:
        # Always serve the expired-screen data fresh; do not move the cursor, so everything
        # missed meanwhile is delivered once the subscription is restored.
        changes: list[dict[str, Any]] = []
        for entity in entities:
            rows = await _select_rows(db, entity, scope, since=-1, limit=1)
            changes.extend(_change(entity, r) for r in await _materialise(db, user, entity, rows))
        return {
            **base,
            "changes": changes,
            "next_cursor": since or _cursor_for(user, 0, pv, now),
            "has_more": False,
            "full_resync_required": False,
            "full_resync_reason": None,
        }

    merged: list[tuple[Entity, dict[str, Any]]] = []
    for entity in entities:
        rows = await _select_rows(db, entity, scope, since=since_seq, limit=limit + 1)
        merged.extend((entity, r) for r in rows)
    merged.sort(key=lambda pair: pair[1]["seq"])
    has_more = len(merged) > limit
    page = merged[:limit]

    changes = []
    for entity in entities:
        rows = [r for e, r in page if e is entity]
        for row in await _materialise(db, user, entity, rows):
            changes.append(_change(entity, row))
    changes.sort(key=lambda c: c["seq"])
    next_seq = changes[-1]["seq"] if changes else since_seq
    return {
        **base,
        "changes": changes,
        "next_cursor": _cursor_for(user, next_seq, pv, now),
        "has_more": has_more,
        "full_resync_required": False,
        "full_resync_reason": None,
    }


# ── snapshot ─────────────────────────────────────────────────────────────────


async def snapshot(
    db: AsyncSession,
    user: User,
    *,
    page_token: str | None,
    limit: int | None,
    branch_id: UUID | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    limit = _clamp(limit)
    pv = permissions_version(user)
    restricted = _restricted(user)
    entities = visible_entities(user, restricted=restricted)
    scope = build_scope(user, branch_id)

    index, after_id = 0, None
    if page_token:
        try:
            token = decode_token(page_token)
        except CursorError:
            raise invalid_cursor() from None
        if (
            token.get("k") != "snap"
            or token.get("b") != str(user.business_id)
            or token.get("u") != str(user.id)
            or token.get("p") != pv
        ):
            raise invalid_cursor()
        start_seq = int(token["c"])
        index = int(token["e"])
        after_id = UUID(token["a"]) if token.get("a") else None
    else:
        # Rows changed while the pages are being read are re-delivered by pulling from here.
        start_seq = await current_seq(db, user.business_id)

    changes: list[dict[str, Any]] = []
    next_token: str | None = None
    while index < len(entities):
        entity = entities[index]
        need = limit - len(changes)
        if need <= 0:
            next_token = encode_token(
                {"v": 1, "k": "snap", "b": str(user.business_id), "u": str(user.id),
                 "c": start_seq, "e": index, "a": str(after_id) if after_id else None, "p": pv}
            )
            break
        rows = await _select_rows(
            db, entity, scope, after_id=after_id, limit=need + 1, snapshot=True
        )
        extra = len(rows) > need
        rows = rows[:need]
        changes.extend(_change(entity, r) for r in await _materialise(db, user, entity, rows))
        if extra:
            next_token = encode_token(
                {"v": 1, "k": "snap", "b": str(user.business_id), "u": str(user.id),
                 "c": start_seq, "e": index, "a": str(rows[-1]["id"]), "p": pv}
            )
            break
        index += 1
        after_id = None

    return {
        "changes": changes,
        "next_page_token": next_token,
        "has_more": next_token is not None,
        "cursor": _cursor_for(user, start_seq, pv, now),
        "server_time": now,
        "permissions_version": pv,
        "entities": [e.name for e in entities],
        "restricted": restricted,
    }

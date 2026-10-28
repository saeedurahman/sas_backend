"""Platform-admin and subscription-state helpers for integration tests."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.core.platform_security import generate_totp_secret, totp_at
from app.services.auth_service import hash_password
from tests.helpers.db import db_execute, db_rows, db_scalar

PLATFORM_PASSWORD = "Platform-Admin-Pass-1"


def root_url(api_base: str) -> str:
    """Server root (platform endpoints live outside /api/v1)."""
    return api_base.removesuffix("/api/v1").rstrip("/")


@dataclass
class AdminCtx:
    id: str
    email: str
    password: str
    totp_secret: str
    root: str
    headers: dict[str, str] | None = None

    def url(self, path: str) -> str:
        return f"{self.root}/platform{path}"


def create_admin_row(root: str, *, email: str | None = None, password: str = PLATFORM_PASSWORD) -> AdminCtx:
    admin_id = str(uuid.uuid4())
    email = email or f"admin-{uuid.uuid4().hex[:10]}@pakpos.test"
    secret = generate_totp_secret()
    db_execute(
        """
        INSERT INTO platform_admins (id, email, name, password_hash, totp_secret)
        VALUES (:id, :email, :name, :hash, :secret)
        """,
        {"id": admin_id, "email": email, "name": "Test Admin", "hash": hash_password(password), "secret": secret},
    )
    return AdminCtx(id=admin_id, email=email, password=password, totp_secret=secret, root=root)


def code_for(admin: AdminCtx, *, offset_steps: int = 0) -> str:
    return totp_at(admin.totp_secret, time.time() + 30 * offset_steps)


def login_response(client: httpx.Client, admin: AdminCtx, *, password: str | None = None,
                   code: str | None = None, fresh_step: bool = True) -> httpx.Response:
    if fresh_step:
        # Each TOTP code works once; tests that sign in repeatedly start from a clean step.
        db_execute("UPDATE platform_admins SET last_totp_step = 0 WHERE id = :id", {"id": admin.id})
    return client.post(
        admin.url("/auth/login"),
        json={
            "email": admin.email,
            "password": password if password is not None else admin.password,
            "totp_code": code if code is not None else code_for(admin),
        },
    )


def sign_in(client: httpx.Client, admin: AdminCtx) -> AdminCtx:
    resp = login_response(client, admin)
    assert resp.status_code == 200, resp.text
    admin.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    return admin


def new_admin(client: httpx.Client, root: str) -> AdminCtx:
    return sign_in(client, create_admin_row(root))


# ── tenant subscription state, set directly in the database ──────────────────


def set_subscription(
    business_id: str,
    *,
    status: str,
    trial_ends_in: timedelta | None = None,
    sub_ends_in: timedelta | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    db_execute(
        """
        UPDATE businesses
           SET subscription_status = :status, trial_ends_at = :trial, subscription_ends_at = :sub
         WHERE id = :id
        """,
        {
            "id": business_id,
            "status": status,
            "trial": None if trial_ends_in is None else now + trial_ends_in,
            "sub": None if sub_ends_in is None else now + sub_ends_in,
        },
    )


def business_row(business_id: str) -> dict[str, Any]:
    return db_rows("SELECT * FROM businesses WHERE id = :id", {"id": business_id})[0]


def audit_rows(action: str, entity_id: str | None = None, business_id: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM platform_audit_log WHERE action = :a"
    params: dict[str, Any] = {"a": action}
    if entity_id:
        sql += " AND entity_id = :e"
        params["e"] = entity_id
    if business_id:
        sql += " AND business_id = :b"
        params["b"] = business_id
    return db_rows(sql + " ORDER BY created_at", params)


def count_tenant_rows(table: str, business_id: str) -> int:
    return int(db_scalar(f"SELECT COUNT(*) FROM {table} WHERE business_id = :b", {"b": business_id}))  # noqa: S608

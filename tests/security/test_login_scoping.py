"""Item 4: phone lookup scoped to a business; no 500s on duplicate phones."""

from __future__ import annotations

import threading
import uuid

import httpx
import pytest

from tests.helpers.server import managed_server
from tests.helpers.tenants import DEFAULT_PASSWORD, register_owner
from tests.security.conftest import login_raw, slug_of

pytestmark = pytest.mark.integration


def _owner(client, api, prefix):
    return register_owner(
        client,
        api,
        uuid.uuid4().int % 10_000_000,
        business_name=f"Scope {uuid.uuid4().hex[:8]}",
        owner_name="Owner",
        phone_prefix=prefix,
    )


def test_login_with_slug_succeeds_and_wrong_slug_is_generic_401(client, sec_api, tenant):
    slug = slug_of(client, tenant)
    ok = login_raw(client, sec_api, tenant.owner_phone, tenant.password, business_slug=slug)
    assert ok.status_code == 200
    assert ok.json()["user"]["business_id"] == tenant.business_id

    bad = login_raw(
        client, sec_api, tenant.owner_phone, tenant.password, business_slug="no-such-biz"
    )
    assert bad.status_code == 401
    assert bad.json()["detail"] == "Invalid credentials"


def test_slug_of_another_business_does_not_authenticate(client, sec_api, tenant):
    other = _owner(client, sec_api, "711")
    r = login_raw(
        client,
        sec_api,
        tenant.owner_phone,
        tenant.password,
        business_slug=slug_of(client, other),
    )
    assert r.status_code == 401


def test_legacy_login_without_slug_still_works(client, sec_api, tenant):
    assert login_raw(client, sec_api, tenant.owner_phone, tenant.password).status_code == 200


def test_post_users_duplicate_phone_across_businesses_is_409(client, sec_api, tenant):
    other = _owner(client, sec_api, "712")
    roles = client.get(other.url("/roles"), headers=other.owner_headers).json()
    r = client.post(
        other.url("/users"),
        headers=other.owner_headers,
        json={
            "full_name": "Dup",
            "phone": tenant.owner_phone,
            "password": DEFAULT_PASSWORD,
            "role_ids": [roles[0]["id"]],
        },
    )
    assert r.status_code == 409


def test_register_with_existing_phone_is_409(client, sec_api, tenant):
    r = client.post(
        f"{sec_api}/auth/register",
        json={
            "business_name": "Dup Biz",
            "business_type_code": "retail",
            "owner_name": "Dup",
            "owner_phone": tenant.owner_phone,
            "owner_password": DEFAULT_PASSWORD,
        },
    )
    assert r.status_code == 409


def test_concurrent_duplicate_user_creation_never_500s(client, sec_api, tenant):
    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    phone = f"719{uuid.uuid4().int % 10_000_000:07d}"
    codes: list[int] = []

    def create():
        with httpx.Client(timeout=60) as c:
            codes.append(
                c.post(
                    tenant.url("/users"),
                    headers=tenant.owner_headers,
                    json={
                        "full_name": "Race",
                        "phone": phone,
                        "password": DEFAULT_PASSWORD,
                        "role_ids": [roles[0]["id"]],
                    },
                ).status_code
            )

    threads = [threading.Thread(target=create) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert 500 not in codes, codes
    assert codes.count(201) == 1, codes


def test_soft_deleted_phone_reuse_is_never_500(client, sec_api, tenant):
    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers).json()
    phone = f"718{uuid.uuid4().int % 10_000_000:07d}"
    payload = {
        "full_name": "Temp",
        "phone": phone,
        "password": DEFAULT_PASSWORD,
        "role_ids": [roles[0]["id"]],
    }
    created = client.post(tenant.url("/users"), headers=tenant.owner_headers, json=payload)
    assert created.status_code == 201
    deleted = client.delete(
        tenant.url(f"/users/{created.json()['id']}"), headers=tenant.owner_headers
    )
    assert deleted.status_code == 200
    again = client.post(tenant.url("/users"), headers=tenant.owner_headers, json=payload)
    assert again.status_code == 409  # the per-business unique constraint still holds the row


def test_slug_required_mode():
    with managed_server({"LOGIN_REQUIRE_BUSINESS_SLUG": "true"}) as api:
        with httpx.Client(timeout=60) as c:
            r = c.post(f"{api}/auth/login", json={"phone": "7100000000", "password": "x"})
            assert r.status_code == 400
            assert "business_slug" in r.json()["detail"]

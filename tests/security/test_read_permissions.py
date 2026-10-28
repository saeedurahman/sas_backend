"""Item 9: read endpoints that used to need only a login now need *.view permissions."""

from __future__ import annotations

import pytest

from tests.helpers.sqlexec import sql

pytestmark = pytest.mark.integration

NEW_KEYS = ("roles.view", "permissions.view", "branches.view")


def _put_setting(client, tenant, key, value=None):
    r = client.put(
        tenant.url("/settings"),
        headers=tenant.owner_headers,
        json={"setting_key": key, "setting_value": value or {"v": 1}},
    )
    assert r.status_code == 200, r.text


def test_new_permissions_seeded_for_default_roles(client, sec_api, tenant):
    rows = sql(
        """
        SELECT lower(r.name), p.permission_key
        FROM role_permissions rp
        JOIN roles r ON r.id = rp.role_id
        JOIN permissions p ON p.id = rp.permission_id
        WHERE r.business_id = :b AND p.permission_key = ANY(:keys)
        """,
        b=tenant.business_id,
        keys=list(NEW_KEYS),
    )
    granted = {(role, key) for role, key in rows}
    for role in ("owner", "manager"):
        for key in NEW_KEYS:
            assert (role, key) in granted
    assert ("cashier", "branches.view") in granted
    assert ("cashier", "roles.view") not in granted
    assert ("cashier", "permissions.view") not in granted


def test_branches_read_requires_branches_view(client, sec_api, tenant):
    for headers in (tenant.owner_headers, tenant.manager_headers, tenant.cashier_headers):
        r = client.get(tenant.url("/branches"), headers=headers)
        assert r.status_code == 200
        one = client.get(tenant.url(f"/branches/{tenant.branch_id}"), headers=headers)
        assert one.status_code == 200
    assert client.get(tenant.url("/branches")).status_code == 401


def test_cashier_loses_roles_and_permission_catalog(client, sec_api, tenant):
    roles = client.get(tenant.url("/roles"), headers=tenant.owner_headers)
    assert roles.status_code == 200
    role_id = roles.json()[0]["id"]

    assert client.get(tenant.url("/roles"), headers=tenant.cashier_headers).status_code == 403
    assert (
        client.get(tenant.url(f"/roles/{role_id}"), headers=tenant.cashier_headers).status_code
        == 403
    )
    assert client.get(tenant.url("/permissions"), headers=tenant.cashier_headers).status_code == 403

    for headers in (tenant.owner_headers, tenant.manager_headers):
        assert client.get(tenant.url("/roles"), headers=headers).status_code == 200
        assert client.get(tenant.url(f"/roles/{role_id}"), headers=headers).status_code == 200
        assert client.get(tenant.url("/permissions"), headers=headers).status_code == 200


def test_settings_reads_are_scoped_for_cashier(client, sec_api, tenant):
    _put_setting(client, tenant, "receipt.footer_text", {"text": "Thanks"})
    _put_setting(client, tenant, "tax.default_label", {"label": "GST"})
    _put_setting(client, tenant, "business.currency", {"code": "PKR"})
    _put_setting(client, tenant, "inventory.fifo_enabled", {"on": True})
    _put_setting(client, tenant, "integrations.gateway_key", {"key": "sensitive"})

    cashier = tenant.cashier_headers
    listing = client.get(tenant.url("/settings"), headers=cashier)
    assert listing.status_code == 200
    keys = {row["setting_key"] for row in listing.json()}
    assert {"receipt.footer_text", "tax.default_label", "business.currency"} <= keys
    assert "inventory.fifo_enabled" not in keys
    assert "integrations.gateway_key" not in keys

    for allowed in ("receipt.footer_text", "tax.default_label", "business.currency"):
        assert client.get(tenant.url(f"/settings/{allowed}"), headers=cashier).status_code == 200
    for blocked in ("integrations.gateway_key", "inventory.fifo_enabled", "anything.else"):
        assert client.get(tenant.url(f"/settings/{blocked}"), headers=cashier).status_code == 403

    # settings.view holders still see everything
    owner_keys = {
        row["setting_key"]
        for row in client.get(tenant.url("/settings"), headers=tenant.owner_headers).json()
    }
    assert "integrations.gateway_key" in owner_keys
    manager = client.get(
        tenant.url("/settings/integrations.gateway_key"), headers=tenant.manager_headers
    )
    assert manager.status_code == 200
    # Writes remain settings.manage only.
    write = client.put(
        tenant.url("/settings"),
        headers=cashier,
        json={"setting_key": "receipt.footer_text", "setting_value": {"text": "x"}},
    )
    assert write.status_code == 403


def test_check_alerts_requires_notifications_manage(client, sec_api, tenant):
    assert (
        client.post(tenant.url("/notifications/check-alerts"), headers=tenant.cashier_headers).status_code
        == 403
    )
    assert client.post(tenant.url("/notifications/check-alerts")).status_code == 401
    ok = client.post(
        tenant.url("/notifications/check-alerts"), headers=tenant.manager_headers
    )
    assert ok.status_code == 200
    assert set(ok.json()) == {"low_stock", "expiry"}

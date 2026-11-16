"""Tax / price settings: API, audit, registration defaults, sync subset, tax-preview."""

from __future__ import annotations

import uuid

import pytest

from tests.helpers.db import db_rows
from tests.helpers.sync import snapshot_all
from tests.helpers.tenants import auth_headers
from tests.integration.tax.helpers import make_rate, put_setting

pytestmark = pytest.mark.integration

DEFAULTS = {
    "tax.enabled": True,
    "tax.prices_include_tax": False,
    "tax.rounding_mode": "none",
    "tax.show_breakdown_on_receipt": True,
    "tax.label": "GST",
    "ui.hide_zero_decimals": False,
}


def _get_all(client, t) -> dict:
    r = client.get(t.url("/settings"), headers=t.owner_headers)
    assert r.status_code == 200, r.text
    return {row["setting_key"]: row["setting_value"]["value"] for row in r.json()}


def test_get_settings_returns_all_tax_keys_with_defaults(http_client, pos_tenant):
    got = _get_all(http_client, pos_tenant)
    for key, value in DEFAULTS.items():
        assert got[key] == value, key


def test_put_roundtrip(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "tax.label", "VAT").status_code == 200
    assert put_setting(http_client, t, "tax.rounding_mode", "nearest_5").status_code == 200
    assert put_setting(http_client, t, "ui.hide_zero_decimals", True).status_code == 200
    got = _get_all(http_client, t)
    assert (got["tax.label"], got["tax.rounding_mode"], got["ui.hide_zero_decimals"]) == (
        "VAT", "nearest_5", True,
    )


@pytest.mark.parametrize(
    "key, value",
    [
        ("tax.rounding_mode", "nearest_3"),
        ("tax.rounding_mode", 5),
        ("tax.enabled", "yes"),
        ("tax.prices_include_tax", 1),
        ("tax.label", ""),
        ("tax.label", "x" * 80),
        ("tax.show_breakdown_on_receipt", "true"),
        ("ui.hide_zero_decimals", None),
    ],
)
def test_put_validates_types(http_client, pos_tenant, key, value):
    assert put_setting(http_client, pos_tenant, key, value).status_code == 422


def test_default_rate_must_belong_to_business(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "tax.default_rate_id", str(uuid.uuid4())).status_code in (404, 422)
    rid = make_rate(http_client, t)
    assert put_setting(http_client, t, "tax.default_rate_id", rid).status_code == 200


def test_settings_manage_permission_required(http_client, pos_tenant):
    t = pos_tenant
    r = put_setting(http_client, t, "tax.label", "X", headers=t.cashier_headers)
    assert r.status_code == 403


def test_audit_entry_for_mode_and_rounding_changes(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "tax.prices_include_tax", True)
    put_setting(http_client, t, "tax.rounding_mode", "nearest_10")
    put_setting(http_client, t, "tax.rounding_mode", "nearest_10")  # unchanged: no new row
    put_setting(http_client, t, "tax.label", "VAT")  # not audited
    rows = db_rows(
        "SELECT old_values, new_values, user_id FROM audit_logs WHERE business_id = :b "
        "AND table_name = 'app_settings' AND new_values->>'event' = 'tax_setting_changed' "
        "ORDER BY created_at",
        {"b": t.business_id},
    )
    assert len(rows) == 2
    assert rows[0]["new_values"]["setting_key"] == "tax.prices_include_tax"
    assert rows[0]["new_values"]["value"] is True
    assert rows[0]["old_values"]["value"] in (False, None)
    assert rows[1]["new_values"]["setting_key"] == "tax.rounding_mode"
    assert rows[1]["new_values"]["value"] == "nearest_10"
    assert rows[1]["new_values"]["applies_to"] == "new_sales_only"
    assert rows[1]["new_values"]["changed_by"]
    assert rows[1]["user_id"] is not None


def test_tax_preview_endpoint(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.get(
        t.url("/settings/tax-preview"), headers=t.owner_headers,
        params={"price": "6", "qty": "3", "rate": "17"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert (b["net_amount"], b["tax_amount"], b["total_amount"]) == ("18.00", "3.06", "21.06")
    assert b["tax_inclusive"] is False

    put_setting(http_client, t, "tax.prices_include_tax", True)
    b = http_client.get(
        t.url("/settings/tax-preview"), headers=t.owner_headers, params={"price": "6", "rate": "17"}
    ).json()
    assert (b["net_amount"], b["tax_amount"], b["total_amount"]) == ("5.13", "0.87", "6.00")

    put_setting(http_client, t, "tax.enabled", False)
    b = http_client.get(
        t.url("/settings/tax-preview"), headers=t.owner_headers, params={"price": "6", "rate": "17"}
    ).json()
    assert (b["tax_amount"], b["total_amount"]) == ("0.00", "6.00")


# ── registration defaults per business type ──────────────────────────────────


def _register(client, api_base, code: str, **extra) -> dict:
    n = uuid.uuid4().int % 10_000_000
    r = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Reg {code} {n}",
            "business_type_code": code,
            "owner_name": "Owner",
            "owner_phone": f"361{n:07d}",
            "owner_password": "TestPass1",
            "branch_name": "Main",
            **extra,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _settings_of(client, api_base, reg) -> dict:
    r = client.get(f"{api_base}/settings", headers=auth_headers(reg["access_token"]))
    assert r.status_code == 200, r.text
    return {row["setting_key"]: row["setting_value"]["value"] for row in r.json()}


@pytest.mark.parametrize(
    "code, inclusive",
    [("pharmacy", True), ("mart", True), ("retail", False), ("restaurant", False)],
)
def test_registration_default_by_business_type(http_client, api_base, code, inclusive):
    s = _settings_of(http_client, api_base, _register(http_client, api_base, code))
    assert s["tax.prices_include_tax"] is inclusive
    assert s["tax.enabled"] is True
    assert s.get("tax.default_rate_id") in (None, "")


def test_registration_explicit_override(http_client, api_base):
    s = _settings_of(http_client, api_base, _register(http_client, api_base, "pharmacy", prices_include_tax=False))
    assert s["tax.prices_include_tax"] is False
    s = _settings_of(http_client, api_base, _register(http_client, api_base, "restaurant", prices_include_tax=True))
    assert s["tax.prices_include_tax"] is True


# ── sync feed subset ─────────────────────────────────────────────────────────


def test_settings_subset_in_sync_snapshot(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "tax.label", "VAT")
    put_setting(http_client, t, "ui.hide_zero_decimals", True)
    changes, _, _ = snapshot_all(http_client, t)
    settings = [c for c in changes if c["entity"] == "settings"]
    assert settings, sorted({c["entity"] for c in changes})
    app = settings[0]["data"]["app_settings"]
    assert app["tax.label"] == "VAT"
    assert app["ui.hide_zero_decimals"] is True
    for key in ("tax.enabled", "tax.prices_include_tax", "tax.rounding_mode", "tax.show_breakdown_on_receipt"):
        assert key in app

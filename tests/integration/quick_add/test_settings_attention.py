"""Quick Add / pricing settings and the products ``needs_attention`` filter."""

from __future__ import annotations

import uuid

import pytest

from tests.helpers.db import db_rows
from tests.helpers.phase2 import new_product, second_branch
from tests.helpers.sync import snapshot_all
from tests.integration.quick_add.helpers import ean13, line, quick_add
from tests.integration.tax.helpers import put_setting

pytestmark = pytest.mark.integration

KEYS = {
    "pricing.default_margin_percent": 15,
    "pricing.suggested_price_rounding": "nearest_1",
    "quick_add.ask_cost": True,
    "quick_add.ask_expiry": False,
    "inventory.require_batch": False,
}


def _all(client, t, headers=None) -> dict:
    r = client.get(t.url("/settings"), headers=headers or t.owner_headers)
    assert r.status_code == 200, r.text
    return {row["setting_key"]: row["setting_value"]["value"] for row in r.json()}


# ── settings ─────────────────────────────────────────────────────────────────


def test_defaults_are_returned_without_rows(http_client, pos_tenant):
    got = _all(http_client, pos_tenant)
    for key, value in KEYS.items():
        assert got[key] == value, key


def test_ask_expiry_defaults_to_true_for_a_pharmacy(http_client, api_base):
    n = uuid.uuid4().int % 10_000_000
    r = http_client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"Pharma {n}",
            "business_type_code": "pharmacy",
            "owner_name": "Owner",
            "owner_phone": f"362{n:07d}",
            "owner_password": "TestPass1",
            "branch_name": "Main",
        },
    )
    assert r.status_code == 201, r.text
    got = http_client.get(
        f"{api_base}/settings", headers={"Authorization": f"Bearer {r.json()['access_token']}"}
    ).json()
    values = {row["setting_key"]: row["setting_value"]["value"] for row in got}
    assert values["quick_add.ask_expiry"] is True
    assert values["quick_add.ask_cost"] is True


def test_put_roundtrip(http_client, pos_tenant):
    t = pos_tenant
    for key, value in {
        "pricing.default_margin_percent": 22.5,
        "pricing.suggested_price_rounding": "nearest_5",
        "quick_add.ask_cost": False,
        "quick_add.ask_expiry": True,
        "inventory.require_batch": True,
    }.items():
        r = put_setting(http_client, t, key, value)
        assert r.status_code == 200, (key, r.text)
        assert _all(http_client, t)[key] == value


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("pricing.default_margin_percent", -1),
        ("pricing.default_margin_percent", 1001),
        ("pricing.default_margin_percent", "abc"),
        ("pricing.default_margin_percent", True),
        ("pricing.default_margin_percent", None),
        ("pricing.suggested_price_rounding", "nearest_7"),
        ("pricing.suggested_price_rounding", 5),
        ("quick_add.ask_cost", "yes"),
        ("quick_add.ask_expiry", 1),
        ("inventory.require_batch", "true"),
    ],
)
def test_invalid_values_are_rejected(http_client, pos_tenant, key, value):
    r = put_setting(http_client, pos_tenant, key, value)
    assert r.status_code == 422, r.text
    assert _all(http_client, pos_tenant)[key] == KEYS[key]  # unchanged


def test_not_wrapped_value_is_rejected(http_client, pos_tenant):
    t = pos_tenant
    r = http_client.put(
        t.url("/settings"),
        headers=t.owner_headers,
        json={"setting_key": "quick_add.ask_cost", "setting_value": False},
    )
    assert r.status_code == 422


def test_margin_zero_and_max_are_allowed(http_client, pos_tenant):
    for v in (0, 1000):
        assert put_setting(http_client, pos_tenant, "pricing.default_margin_percent", v).status_code == 200


def test_settings_are_business_wide_not_per_branch(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    r = http_client.put(
        t.url("/settings"),
        headers=t.owner_headers,
        json={"setting_key": "quick_add.ask_cost", "setting_value": {"value": False}, "branch_id": other},
    )
    assert r.status_code == 422


def test_cashier_can_read_but_not_write(http_client, pos_tenant):
    t = pos_tenant
    assert put_setting(http_client, t, "quick_add.ask_cost", False).status_code == 200
    seen = _all(http_client, t, headers=t.cashier_headers)
    assert seen["quick_add.ask_cost"] is False
    assert seen["pricing.default_margin_percent"] == 15
    r = put_setting(http_client, t, "quick_add.ask_cost", True, headers=t.cashier_headers)
    assert r.status_code == 403


def test_changes_are_audited_with_before_after_and_actor(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "pricing.default_margin_percent", 20)
    put_setting(http_client, t, "pricing.default_margin_percent", 20)  # unchanged: no new row
    put_setting(http_client, t, "quick_add.ask_cost", False)
    rows = db_rows(
        "SELECT old_values, new_values, user_id FROM audit_logs WHERE business_id = :b "
        "AND table_name = 'app_settings' AND new_values->>'event' = 'quick_add_setting_changed' "
        "ORDER BY created_at",
        {"b": t.business_id},
    )
    assert len(rows) == 2
    assert rows[0]["new_values"]["setting_key"] == "pricing.default_margin_percent"
    assert rows[0]["old_values"]["value"] == 15 and rows[0]["new_values"]["value"] == 20
    assert rows[1]["new_values"]["setting_key"] == "quick_add.ask_cost"
    assert rows[1]["old_values"]["value"] is True and rows[1]["new_values"]["value"] is False
    me = http_client.get(t.url("/auth/me"), headers=t.owner_headers).json()["id"]
    assert all(str(r["user_id"]) == me and r["new_values"]["changed_by"] == me for r in rows)


def test_settings_are_in_the_sync_settings_subset(http_client, pos_tenant):
    t = pos_tenant
    put_setting(http_client, t, "pricing.default_margin_percent", 18)
    put_setting(http_client, t, "inventory.require_batch", True)
    changes, _, _ = snapshot_all(http_client, t)
    settings = [c for c in changes if c["entity"] == "settings"]
    assert settings, sorted({c["entity"] for c in changes})
    app = settings[0]["data"]["app_settings"]
    assert app["pricing.default_margin_percent"] == 18
    assert app["pricing.suggested_price_rounding"] == "nearest_1"
    assert app["quick_add.ask_cost"] is True
    assert app["quick_add.ask_expiry"] is False
    assert app["inventory.require_batch"] is True


# ── needs_attention ──────────────────────────────────────────────────────────


def _list(client, t, **params) -> list[dict]:
    r = client.get(t.url("/products"), headers=t.owner_headers, params={"limit": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()["items"]


def test_needs_attention_filter_and_flags(http_client, pos_tenant):
    t = pos_tenant
    cat = http_client.post(
        t.url("/categories"), headers=t.owner_headers, json={"name": f"C{uuid.uuid4().hex[:5]}"}
    ).json()["id"]
    res = quick_add(
        http_client,
        t,
        [
            line(cid="all_ok", category_id=cat, image_url="https://img.example/a.jpg", sale_price="10"),
            line(cid="no_cat", image_url="https://img.example/b.jpg", sale_price="10"),
            line(cid="no_img", category_id=cat, sale_price="10"),
            line(cid="no_price", category_id=cat, image_url="https://img.example/c.jpg", sale_price="0"),
            line(cid="nothing", sale_price="0"),
        ],
    )
    pid = {ln["client_line_id"]: ln["product_id"] for ln in res["lines"]}
    flagged = {p["id"]: p for p in _list(http_client, t, needs_attention="true")}
    assert pid["all_ok"] not in flagged
    assert flagged[pid["no_cat"]]["missing"] == ["category"]
    assert flagged[pid["no_img"]]["missing"] == ["image"]
    assert flagged[pid["no_price"]]["missing"] == ["price"]
    assert flagged[pid["nothing"]]["missing"] == ["category", "image", "price"]
    assert all(p["needs_attention"] is True for p in flagged.values())

    complete = {p["id"]: p for p in _list(http_client, t, needs_attention="false")}
    assert pid["all_ok"] in complete
    assert complete[pid["all_ok"]]["needs_attention"] is False and complete[pid["all_ok"]]["missing"] == []
    assert not (set(complete) & set(flagged))

    # unfiltered list carries the computed fields too
    everything = {p["id"]: p for p in _list(http_client, t)}
    assert everything[pid["nothing"]]["needs_attention"] is True
    assert everything[pid["all_ok"]]["needs_attention"] is False


def test_detail_has_needs_attention(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(http_client, t, [line(sale_price="0")])
    p = http_client.get(t.url(f"/products/{res['lines'][0]['product_id']}"), headers=t.owner_headers).json()
    assert p["needs_attention"] is True and p["missing"] == ["category", "image", "price"]


def test_pricing_a_product_clears_the_price_flag(http_client, pos_tenant):
    t = pos_tenant
    res = quick_add(http_client, t, [line(sale_price="0")])
    pid = res["lines"][0]["product_id"]
    assert "price" in http_client.get(t.url(f"/products/{pid}"), headers=t.owner_headers).json()["missing"]
    # a re-scan with update_existing_prices sets the price through the price service
    quick_add(http_client, t, [line(res["lines"][0] and _barcode_of(t, pid), qty="0", sale_price="25", update_existing_prices=True)])
    got = http_client.get(t.url(f"/products/{pid}"), headers=t.owner_headers).json()
    assert "price" not in got["missing"]


def _barcode_of(t, product_id: str) -> str:
    return db_rows("SELECT barcode FROM barcodes WHERE product_id = :p", {"p": product_id})[0]["barcode"]


def test_filter_composes_with_search_and_paging(http_client, pos_tenant):
    t = pos_tenant
    tag = uuid.uuid4().hex[:8]
    quick_add(http_client, t, [line(name=f"Needs {tag} {i}", sale_price="0") for i in range(3)])
    items = _list(http_client, t, needs_attention="true", search=tag)
    assert len(items) == 3
    page = http_client.get(
        t.url("/products"),
        headers=t.owner_headers,
        params={"needs_attention": "true", "search": tag, "limit": 2},
    ).json()
    assert page["total"] == 3 and len(page["items"]) == 2


def test_plain_product_create_still_works_and_is_flagged(http_client, pos_tenant):
    t = pos_tenant
    pid = new_product(http_client, t)
    flagged = {p["id"] for p in _list(http_client, t, needs_attention="true")}
    assert pid in flagged


def test_old_clients_other_fields_unchanged(http_client, pos_tenant):
    t = pos_tenant
    items = _list(http_client, t)
    assert items and {"id", "name", "needs_attention", "missing"} <= set(items[0])
    assert ean13()  # helper sanity

"""Trial / grace / expiry / suspension enforced inside get_current_user."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.db import db_execute, db_scalar
from tests.helpers.platform import business_row, root_url, set_subscription
from tests.helpers.tenants import build_pos_tenant, login, register_owner

pytestmark = pytest.mark.integration

DAY = timedelta(days=1)
CONTACT_KEYS = ("contact_phone", "contact_whatsapp", "contact_email")


@pytest.fixture
def tenant(http_client, api_base, unique_suffix):
    return build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="520")


@pytest.fixture
def contact(http_client):
    """Set known support contact values; restore afterwards."""
    saved = {
        k: db_scalar("SELECT value FROM platform_settings WHERE key = :k", {"k": k})
        for k in (*CONTACT_KEYS, "payment_instructions")
    }
    values = {
        "contact_phone": "0300-1112223",
        "contact_whatsapp": "0300-4445556",
        "contact_email": "help@pakpos.test",
        "payment_instructions": "JazzCash 0300-0000000",
    }
    for key, value in values.items():
        db_execute(
            "INSERT INTO platform_settings (key, value) VALUES (:k, :v) "
            "ON CONFLICT (key) DO UPDATE SET value = :v",
            {"k": key, "v": value},
        )
    yield values
    for key, value in saved.items():
        db_execute("UPDATE platform_settings SET value = :v WHERE key = :k", {"k": key, "v": value or ""})


def _get(client, t, path, **kw):
    return client.get(t.url(path), headers=t.owner_headers, **kw)


def _post(client, t, path, body=None):
    return client.post(t.url(path), headers=t.owner_headers, json=body or {})


# ── registration ──────────────────────────────────────────────────────────────


def test_registration_starts_a_14_day_trial(http_client, api_base, unique_suffix):
    t = register_owner(http_client, api_base, unique_suffix, business_name=f"Trial {unique_suffix}",
                       owner_name="Owner", phone_prefix="521")
    row = business_row(t.business_id)
    assert row["subscription_status"] == "trial"
    delta = row["trial_ends_at"] - datetime.now(timezone.utc)
    assert timedelta(days=13, hours=23) < delta <= timedelta(days=14)
    body = http_client.post(
        f"{api_base}/auth/login", json={"phone": t.owner_phone, "password": t.password}
    ).json()
    assert body["subscription"]["status"] == "trial"
    assert body["subscription"]["days_left"] == 14


def test_every_feature_works_on_trial_without_plan_gating(http_client, tenant):
    # restaurant/accounting/manufacturing are per-business switches, never plan-gated;
    # nothing about the trial plan blocks creating a second and third branch.
    for n in range(2):
        r = _post(http_client, tenant, "/branches", {"name": f"Extra {n}"})
        assert r.status_code == 201, r.text
    assert len(_get(http_client, tenant, "/branches").json()) >= 3


# ── 402 bodies ────────────────────────────────────────────────────────────────


def test_expired_trial_is_402_with_contact(http_client, tenant, contact):
    set_subscription(tenant.business_id, status="trial", trial_ends_in=-DAY)
    r = _get(http_client, tenant, "/products")
    assert r.status_code == 402
    body = r.json()
    assert body["code"] == "subscription_expired"
    assert "trial" in body["message"].lower()
    assert body["expired_at"] and body["expired_at"].startswith(
        (datetime.now(timezone.utc) - DAY).strftime("%Y-%m-%d")
    )
    assert body["contact"] == {"phone": "0300-1112223", "whatsapp": "0300-4445556", "email": "help@pakpos.test"}
    assert set(body) >= {"code", "message", "expired_at", "contact"}
    # writes are blocked too, with the same code
    assert _post(http_client, tenant, "/customers", {"name": "x"}).json()["code"] == "subscription_expired"


def test_active_past_grace_is_402_expired(http_client, tenant):
    set_subscription(tenant.business_id, status="active", sub_ends_in=-timedelta(days=3, minutes=5))
    r = _get(http_client, tenant, "/products")
    assert r.status_code == 402 and r.json()["code"] == "subscription_expired"


@pytest.mark.parametrize(
    "stored,code",
    [("suspended", "subscription_suspended"), ("cancelled", "subscription_cancelled")],
)
def test_suspended_and_cancelled_are_402_even_with_time_left(http_client, tenant, stored, code):
    set_subscription(tenant.business_id, status=stored, sub_ends_in=30 * DAY)
    r = _get(http_client, tenant, "/products")
    assert r.status_code == 402 and r.json()["code"] == code
    assert r.json()["expired_at"] is None


def test_inactive_business_is_403_not_402(http_client, tenant):
    db_execute("UPDATE businesses SET is_active = FALSE WHERE id = :b", {"b": tenant.business_id})
    r = _get(http_client, tenant, "/products")
    assert r.status_code == 403


def test_other_tenants_are_unaffected(http_client, tenant, api_base, unique_suffix):
    other = build_pos_tenant(http_client, api_base, unique_suffix + 1, phone_prefix="522")
    set_subscription(tenant.business_id, status="suspended")
    assert _get(http_client, tenant, "/products").status_code == 402
    assert _get(http_client, other, "/products").status_code == 200


# ── read-only grace ───────────────────────────────────────────────────────────


@pytest.fixture
def in_grace(tenant):
    set_subscription(tenant.business_id, status="active", sub_ends_in=-DAY)
    return tenant


def test_grace_allows_reads_blocks_writes(http_client, in_grace, contact):
    t = in_grace
    assert _get(http_client, t, "/products").status_code == 200
    assert _get(http_client, t, "/sales").status_code == 200
    for method, path, body in (
        ("POST", "/customers", {"name": "Blocked"}),
        ("PUT", "/settings", {"setting_key": "x", "setting_value": "y"}),
        ("DELETE", "/products/00000000-0000-0000-0000-000000000001", None),
        ("PUT", "/customers/00000000-0000-0000-0000-000000000001", {"name": "x"}),
    ):
        r = http_client.request(method, t.url(path), headers=t.owner_headers, json=body)
        assert r.status_code == 402, (method, path, r.text)
        data = r.json()
        assert data["code"] == "subscription_grace_readonly"
        assert data["expired_at"]
        assert data["contact"]["phone"] == "0300-1112223"
        assert "read-only" in data["message"]


def test_grace_blocks_selling_but_never_deletes_data(http_client, in_grace):
    t = in_grace
    before = (
        db_scalar("SELECT COUNT(*) FROM stock_movements WHERE business_id = :b", {"b": t.business_id}),
        db_scalar("SELECT COUNT(*) FROM products WHERE business_id = :b", {"b": t.business_id}),
    )
    r = _post(http_client, t, "/sales", {"branch_id": t.branch_id, "lines": [], "payments": []})
    assert r.status_code == 402 and r.json()["code"] == "subscription_grace_readonly"
    after = (
        db_scalar("SELECT COUNT(*) FROM stock_movements WHERE business_id = :b", {"b": t.business_id}),
        db_scalar("SELECT COUNT(*) FROM products WHERE business_id = :b", {"b": t.business_id}),
    )
    assert before == after and before[1] >= 1


def test_shift_close_is_allowed_during_grace(http_client, in_grace):
    t = in_grace
    r = _post(http_client, t, f"/shifts/{t.shift_id}/close", {"actual_cash": "0"})
    assert r.status_code == 200, r.text
    # ...but other shift writes are not
    r2 = _post(http_client, t, f"/shifts/{t.shift_id}/cash-movement", {"tx_type": "cash_in", "amount": "5"})
    assert r2.status_code == 402


def test_shift_close_is_blocked_once_fully_expired(http_client, tenant):
    set_subscription(tenant.business_id, status="active", sub_ends_in=-10 * DAY)
    r = _post(http_client, tenant, f"/shifts/{tenant.shift_id}/close", {"actual_cash": "0"})
    assert r.status_code == 402 and r.json()["code"] == "subscription_expired"


# ── whitelist ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["expired", "suspended", "cancelled", "grace"])
def test_whitelisted_routes_work_in_every_state(http_client, api_base, tenant, state):
    if state == "expired":
        set_subscription(tenant.business_id, status="trial", trial_ends_in=-DAY)
    elif state == "grace":
        set_subscription(tenant.business_id, status="active", sub_ends_in=-DAY)
    else:
        set_subscription(tenant.business_id, status=state, sub_ends_in=5 * DAY)

    me = _get(http_client, tenant, "/auth/me")
    assert me.status_code == 200, me.text
    assert me.json()["subscription"]["status"] == state
    st = _get(http_client, tenant, "/subscription/status")
    assert st.status_code == 200
    assert st.json()["status"] == state
    assert st.json()["in_grace"] is (state == "grace")

    # logout needs the refresh token; sign in to get one
    body = http_client.post(
        f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password}
    )
    assert body.status_code == 200
    data = body.json()
    assert data["subscription"]["status"] == state
    out = http_client.post(
        tenant.url("/auth/logout"),
        headers={"Authorization": f"Bearer {data['access_token']}"},
        json={"refresh_token": data["refresh_token"]},
    )
    assert out.status_code == 200, out.text


# ── /subscription/status ──────────────────────────────────────────────────────


def test_subscription_status_shape_for_trial(http_client, tenant, contact):
    r = _get(http_client, tenant, "/subscription/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "plan_name", "status", "trial_ends_at", "subscription_ends_at", "days_left",
        "in_grace", "contact", "payment_instructions",
    }
    assert body["status"] == "trial" and body["in_grace"] is False
    assert body["days_left"] in (13, 14)
    assert body["trial_ends_at"] and body["subscription_ends_at"] is None
    assert body["contact"] == {"phone": "0300-1112223", "whatsapp": "0300-4445556", "email": "help@pakpos.test"}
    assert body["payment_instructions"] == "JazzCash 0300-0000000"


def test_subscription_status_is_open_to_any_logged_in_user(http_client, tenant):
    r = http_client.get(tenant.url("/subscription/status"), headers=tenant.cashier_headers)
    assert r.status_code == 200
    assert http_client.get(tenant.url("/subscription/status")).status_code == 401


def test_subscription_status_active_with_plan_name(http_client, tenant):
    plan_id = db_scalar("SELECT id FROM plans WHERE name = 'Basic'")
    db_execute("UPDATE businesses SET plan_id = :p WHERE id = :b", {"p": plan_id, "b": tenant.business_id})
    set_subscription(tenant.business_id, status="active", sub_ends_in=10 * DAY)
    body = _get(http_client, tenant, "/subscription/status").json()
    assert body["plan_name"] == "Basic" and body["status"] == "active" and body["days_left"] in (9, 10)


def test_subscription_status_in_grace(http_client, in_grace):
    body = _get(http_client, in_grace, "/subscription/status").json()
    assert body["status"] == "grace" and body["in_grace"] is True and body["days_left"] < 0


# ── sign-in paths for expired businesses ──────────────────────────────────────


def test_login_pin_login_and_refresh_work_for_expired_business(http_client, api_base, tenant):
    users = {u["phone"]: u["id"] for u in _get(http_client, tenant, "/users").json()}
    cashier_id = users[tenant.cashier_phone]
    assert http_client.put(
        tenant.url(f"/users/{cashier_id}/pin"), headers=tenant.owner_headers, json={"pin_code": "493827"}
    ).status_code == 200
    slug = _get(http_client, tenant, "/auth/me").json()["business_slug"]

    first = http_client.post(f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password}).json()
    set_subscription(tenant.business_id, status="trial", trial_ends_in=-2 * DAY)

    pw = http_client.post(f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password})
    assert pw.status_code == 200
    assert pw.json()["subscription"]["status"] == "expired"
    assert pw.json()["subscription"]["days_left"] <= -2

    pin = http_client.post(
        f"{api_base}/auth/login/pin", json={"business_slug": slug, "user_id": cashier_id, "pin_code": "493827"}
    )
    assert pin.status_code == 200 and pin.json()["subscription"]["status"] == "expired"
    # ...and their token is told 402 on real work
    tok = {"Authorization": f"Bearer {pin.json()['access_token']}"}
    assert http_client.get(tenant.url("/products"), headers=tok).status_code == 402

    ref = http_client.post(f"{api_base}/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert ref.status_code == 200
    assert ref.json()["subscription"]["status"] == "expired"


def test_refresh_and_logins_reject_an_inactive_business(http_client, api_base, tenant):
    first = http_client.post(f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password}).json()
    db_execute("UPDATE businesses SET is_active = FALSE WHERE id = :b", {"b": tenant.business_id})
    assert http_client.post(f"{api_base}/auth/refresh", json={"refresh_token": first["refresh_token"]}).status_code == 401
    assert http_client.post(
        f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password}
    ).status_code == 403


def test_refresh_reports_current_subscription_when_healthy(http_client, api_base, tenant):
    first = http_client.post(f"{api_base}/auth/login", json={"phone": tenant.owner_phone, "password": tenant.password}).json()
    ref = http_client.post(f"{api_base}/auth/refresh", json={"refresh_token": first["refresh_token"]}).json()
    assert ref["subscription"]["status"] == "trial"


# ── contact comes from the editable settings table ────────────────────────────


def test_contact_in_402_follows_platform_settings(http_client, tenant, contact):
    set_subscription(tenant.business_id, status="suspended")
    assert _get(http_client, tenant, "/products").json()["contact"]["phone"] == "0300-1112223"
    db_execute("UPDATE platform_settings SET value = '0311-0000000' WHERE key = 'contact_phone'")
    assert _get(http_client, tenant, "/products").json()["contact"]["phone"] == "0311-0000000"


def test_402_has_empty_contact_strings_when_nothing_is_configured(http_client, tenant):
    for key in CONTACT_KEYS:
        db_execute("UPDATE platform_settings SET value = '' WHERE key = :k", {"k": key})
    set_subscription(tenant.business_id, status="suspended")
    assert _get(http_client, tenant, "/products").json()["contact"] == {"phone": "", "whatsapp": "", "email": ""}


# ── reactivation keeps all data ───────────────────────────────────────────────


def test_renewal_restores_access_with_data_intact(http_client, tenant):
    sales_before = _get(http_client, tenant, "/products").json()
    set_subscription(tenant.business_id, status="active", sub_ends_in=-10 * DAY)
    assert _get(http_client, tenant, "/products").status_code == 402
    set_subscription(tenant.business_id, status="active", sub_ends_in=30 * DAY)
    assert _get(http_client, tenant, "/products").json() == sales_before


def test_existing_businesses_were_migrated_to_active(http_client):
    # the migration's backfill statement, replayed inside a rolled-back transaction
    import importlib.util
    from pathlib import Path

    from sqlalchemy import create_engine, text

    from tests.helpers.db import sync_db_url

    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "018_subscription_platform.py"
    spec = importlib.util.spec_from_file_location("mig018", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(sync_db_url())
    with engine.connect() as conn:
        trans = conn.begin()
        biz = conn.execute(text("SELECT id FROM businesses WHERE deleted_at IS NULL LIMIT 1")).scalar_one()
        conn.execute(text(
            "UPDATE businesses SET subscription_status = 'trial', subscription_ends_at = NULL, "
            "plan_id = NULL WHERE id = :id"), {"id": biz})
        conn.execute(text(migration.BACKFILL_SQL))
        row = conn.execute(text(
            "SELECT subscription_status, subscription_ends_at - NOW(), "
            "(SELECT name FROM plans WHERE id = plan_id) FROM businesses WHERE id = :id"), {"id": biz}).one()
        trans.rollback()
    assert row[0] == "active"
    assert timedelta(days=364) < row[1] <= timedelta(days=365)
    assert row[2] == "Basic"


def test_default_plans_and_settings_are_seeded():
    names = {r for r in (db_scalar("SELECT name FROM plans WHERE name = :n", {"n": n}) for n in ("Basic", "Multi-branch")) if r}
    assert names == {"Basic", "Multi-branch"}
    keys = {k for k in ("contact_phone", "contact_whatsapp", "contact_email", "payment_instructions")
            if db_scalar("SELECT 1 FROM platform_settings WHERE key = :k", {"k": k})}
    assert len(keys) == 4

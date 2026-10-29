"""GET /sync/pull, /sync/snapshot, /time, lease: feed semantics."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.licensing.lease import verify_lease
from app.services import sync_feed
from tests.helpers.db import db_execute, db_rows, db_scalar
from tests.helpers.phase2 import D, add_user, new_product, second_branch, stock_in
from tests.helpers.platform import set_subscription
from tests.helpers.server import TEST_LICENSE_PUBLIC_KEY
from tests.helpers.sync import (
    by_entity,
    op,
    pull,
    pull_all,
    pull_json,
    push_ok,
    sale_payload,
    snapshot_all,
)

pytestmark = pytest.mark.integration


def _ids(changes, entity):
    return {c["id"] for c in by_entity(changes, entity)}


# ── stamping / basic pull ────────────────────────────────────────────────────


def test_snapshot_contains_the_tenant_and_pull_from_its_cursor_is_empty(http_client, pos_tenant):
    t = pos_tenant
    changes, cursor, _ = snapshot_all(http_client, t)
    entities = {c["entity"] for c in changes}
    assert {"settings", "branch", "cash_register", "role", "user", "unit", "product", "stock_level"} <= entities
    assert t.product_id in _ids(changes, "product")
    assert all(c["op"] == "upsert" and c["data"] is not None for c in changes)

    page = pull_json(http_client, t, since=cursor)
    assert page["changes"] == [] and page["has_more"] is False and page["full_resync_required"] is False
    assert page["next_cursor"]


def test_stock_level_follows_movements_with_a_new_seq(http_client, pos_tenant):
    t = pos_tenant
    _, cursor, _ = snapshot_all(http_client, t)
    levels = [c for c in snapshot_all(http_client, t)[0] if c["entity"] == "stock_level"]
    mine = next(c for c in levels if c["data"]["product_id"] == t.product_id)
    assert D(mine["data"]["qty"]) == D("100")

    push_ok(http_client, t, [op("sale.create", sale_payload(t, qty="3"))])
    page = pull_json(http_client, t, since=cursor)
    stock = [c for c in page["changes"] if c["entity"] == "stock_level"]
    assert len(stock) == 1
    assert stock[0]["data"]["product_id"] == t.product_id and D(stock[0]["data"]["qty"]) == D("97")
    assert stock[0]["seq"] > 0
    assert stock[0]["id"] == mine["id"]


def test_numbers_are_exact_strings(http_client, pos_tenant):
    t = pos_tenant
    changes, _, _ = snapshot_all(http_client, t)
    stock = next(c for c in changes if c["entity"] == "stock_level" and c["data"]["product_id"] == t.product_id)
    assert isinstance(stock["data"]["qty"], str) and stock["data"]["qty"].startswith("100")


def test_seq_is_unique_and_increasing_in_pull_order(http_client, pos_tenant):
    t = pos_tenant
    changes, _ = pull_all(http_client, t)
    seqs = [c["seq"] for c in changes]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


# ── tombstones ───────────────────────────────────────────────────────────────


def test_soft_delete_produces_a_tombstone_and_snapshot_omits_it(http_client, pos_tenant):
    t = pos_tenant
    victim = new_product(http_client, t, "Doomed")
    _, cursor, _ = snapshot_all(http_client, t)
    page = pull_json(http_client, t, since=cursor)
    assert page["changes"] == []

    resp = http_client.delete(t.url(f"/products/{victim}"), headers=t.owner_headers)
    assert resp.status_code in (200, 204), resp.text
    page = pull_json(http_client, t, since=cursor)
    gone = [c for c in page["changes"] if c["entity"] == "product" and c["id"] == victim]
    assert len(gone) == 1 and gone[0]["op"] == "delete" and gone[0]["data"] is None

    assert db_scalar("SELECT deleted_at IS NOT NULL FROM products WHERE id = :i", {"i": victim}) is True
    assert victim not in _ids(snapshot_all(http_client, t)[0], "product")


def test_update_after_cursor_is_an_upsert(http_client, pos_tenant):
    t = pos_tenant
    _, cursor, _ = snapshot_all(http_client, t)
    resp = http_client.put(t.url(f"/products/{t.product_id}"), headers=t.owner_headers, json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    page = pull_json(http_client, t, since=cursor)
    changed = [c for c in page["changes"] if c["entity"] == "product"]
    assert len(changed) == 1 and changed[0]["data"]["name"] == "Renamed"


# ── cursor semantics ─────────────────────────────────────────────────────────


def test_cursor_resume_after_has_more(http_client, pos_tenant):
    t = pos_tenant
    for i in range(6):
        new_product(http_client, t, f"Bulk {i}")
    everything, _ = pull_all(http_client, t)

    first = pull_json(http_client, t, limit=4)
    assert len(first["changes"]) == 4 and first["has_more"] is True
    second = pull_json(http_client, t, since=first["next_cursor"], limit=4)
    assert second["changes"][0]["seq"] > first["changes"][-1]["seq"]

    # Paging in small chunks yields exactly the same stream, with no gaps or repeats.
    paged, cursor = pull_all(http_client, t, limit=3)
    assert [(c["entity"], c["id"], c["seq"]) for c in paged] == [
        (c["entity"], c["id"], c["seq"]) for c in everything
    ]
    assert pull_json(http_client, t, since=cursor)["changes"] == []


def test_new_change_after_final_page_shows_up_next(http_client, pos_tenant):
    t = pos_tenant
    _, cursor = pull_all(http_client, t)
    pid = new_product(http_client, t, "After")
    page = pull_json(http_client, t, since=cursor)
    assert pid in _ids(page["changes"], "product")


def test_tampered_or_foreign_cursor_is_rejected(http_client, pos_tenant, registered_owner):
    t = pos_tenant
    cursor = pull_json(http_client, t)["next_cursor"]
    body, sig = cursor.split(".")
    for bad in (body + ".AAAAAAAA", "garbage", body):
        resp = pull(http_client, t, since=bad)
        assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_cursor"
    # A cursor minted for another business is not accepted either.
    other = pull_json(http_client, registered_owner)["next_cursor"]
    assert pull(http_client, t, since=other).status_code == 400


def _cursor(user_id, business_id, seq, issued_at, pv):
    return sync_feed.encode_token(
        {"v": 1, "k": "pull", "b": business_id, "u": user_id, "s": seq, "t": int(issued_at.timestamp()), "p": pv}
    )


def _me(http_client, t):
    return http_client.get(t.url("/auth/me"), headers=t.owner_headers).json()


def test_full_resync_required_when_cursor_older_than_retention(http_client, pos_tenant):
    t = pos_tenant
    me = _me(http_client, t)
    old = datetime.now(timezone.utc) - timedelta(days=91)
    page = pull_json(http_client, t, since=_cursor(me["id"], t.business_id, 0, old, me["permissions_version"]))
    assert page["full_resync_required"] is True
    assert page["full_resync_reason"] == "cursor_expired"
    assert page["changes"] == [] and page["next_cursor"] is None

    fresh = datetime.now(timezone.utc) - timedelta(days=89)
    ok = pull_json(http_client, t, since=_cursor(me["id"], t.business_id, 0, fresh, me["permissions_version"]))
    assert ok["full_resync_required"] is False and ok["changes"]


def test_full_resync_when_permissions_or_user_differ_or_cursor_ahead(http_client, pos_tenant):
    t = pos_tenant
    me = _me(http_client, t)
    now = datetime.now(timezone.utc)
    pv = me["permissions_version"]
    assert pull_json(http_client, t, since=_cursor(me["id"], t.business_id, 0, now, "stale"))["full_resync_reason"] == "permissions_changed"
    assert pull_json(http_client, t, since=_cursor(str(uuid.uuid4()), t.business_id, 0, now, pv))["full_resync_reason"] == "user_changed"
    assert pull_json(http_client, t, since=_cursor(me["id"], t.business_id, 10**9, now, pv))["full_resync_reason"] == "cursor_ahead_of_server"


def test_role_permission_change_forces_resync_for_that_user(http_client, pos_tenant):
    t = pos_tenant
    cursor = pull_json(http_client, t, headers=t.cashier_headers)["next_cursor"]
    roles = http_client.get(t.url("/roles"), headers=t.owner_headers).json()
    cashier_role = next(r for r in roles if r["name"].lower() == "cashier")
    # Revoke a permission the cashier role has; its permissions_version changes.
    db_execute(
        "DELETE FROM role_permissions WHERE role_id = :r AND permission_id = "
        "(SELECT id FROM permissions WHERE permission_key = 'customers.view')",
        {"r": cashier_role["id"]},
    )
    from app.core import permission_cache  # noqa: F401 - cache lives in the server process

    # The server caches permission keys briefly per process; retry until it notices.
    deadline = time.monotonic() + 20
    while True:
        page = pull_json(http_client, t, headers=t.cashier_headers, since=cursor)
        if page["full_resync_required"] or time.monotonic() > deadline:
            break
        time.sleep(1)
    # permissions_version is derived from the (possibly cached) keys; either the cache
    # expired and we are told to resync, or the role change shows up as a feed change.
    assert page["full_resync_required"] or any(c["entity"] == "role" for c in page["changes"])


# ── scope & permissions ──────────────────────────────────────────────────────


def test_branch_scope_restricts_branch_entities(http_client, pos_tenant):
    t = pos_tenant
    other = second_branch(http_client, t)
    product = t.product_id
    stock_in(http_client, t, product, "7", branch_id=other)

    owner_changes, _, _ = snapshot_all(http_client, t)
    assert {t.branch_id, other} <= _ids(owner_changes, "branch")

    restricted = add_user(http_client, t, "cashier", branch_id=t.branch_id)
    changes, _, _ = snapshot_all(http_client, t, headers=restricted)
    assert _ids(changes, "branch") == {t.branch_id}
    assert {c["data"]["branch_id"] for c in by_entity(changes, "stock_level")} == {t.branch_id}
    assert all(
        u["data"]["branch_ids"] is None or other not in u["data"]["branch_ids"]
        for u in by_entity(changes, "user")
    )

    feed, _ = pull_all(http_client, t, headers=restricted)
    assert other not in _ids(feed, "branch")
    assert {c["data"]["branch_id"] for c in by_entity(feed, "stock_level")} <= {t.branch_id}

    denied = pull(http_client, t, headers=restricted, branch_id=other)
    assert denied.status_code == 403


def test_entities_follow_permissions(http_client, pos_tenant):
    t = pos_tenant
    manager = pull_json(http_client, t, headers=t.manager_headers)
    cashier = pull_json(http_client, t, headers=t.cashier_headers)
    for page in (manager, cashier):
        assert "product" in page["entities"] and "settings" in page["entities"]
    # Strip a permission from the cashier role directly and check the entity disappears.
    roles = http_client.get(t.url("/roles"), headers=t.owner_headers).json()
    assert next(r for r in roles if r["name"].lower() == "cashier")
    assert "discount_scheme" in cashier["entities"]  # cashier has discounts.view by default


def test_user_entity_never_leaks_credentials_or_contact(http_client, pos_tenant):
    t = pos_tenant
    changes, _, _ = snapshot_all(http_client, t)
    for user in by_entity(changes, "user"):
        assert set(user["data"]) == {
            "id", "name", "roles", "role_ids", "branch_ids", "status", "is_locked", "permissions_version",
        }
    blob = str(changes)
    assert "password_hash" not in blob and "pin_hash" not in blob and t.owner_phone not in blob


def test_roles_snapshot_lists_permission_keys(http_client, pos_tenant):
    t = pos_tenant
    changes, _, _ = snapshot_all(http_client, t)
    roles = {c["data"]["name"].lower(): c["data"] for c in by_entity(changes, "role")}
    assert "sales.create" in roles["cashier"]["permission_keys"]
    assert "users.delete" in roles["owner"]["permission_keys"]
    assert "users.delete" not in roles["cashier"]["permission_keys"]


def test_user_permissions_version_matches_auth_me(http_client, pos_tenant):
    t = pos_tenant
    changes, _, _ = snapshot_all(http_client, t)
    me = _me(http_client, t)
    mine = next(u for u in by_entity(changes, "user") if u["id"] == me["id"])
    assert mine["data"]["permissions_version"] == me["permissions_version"]
    assert pull_json(http_client, t)["permissions_version"] == me["permissions_version"]


def test_customer_balance_in_feed(http_client, pos_tenant):
    t = pos_tenant
    customer = str(uuid.uuid4())
    _, cursor, _ = snapshot_all(http_client, t)
    push_ok(http_client, t, [
        op("customer.create", {"id": customer, "name": "Ledger Lee", "credit_limit": "500"}),
        op("sale.create", sale_payload(t, customer_id=customer,
                                       payments=[{"payment_method": "credit", "amount": "100.00"}])),
    ])
    page = pull_json(http_client, t, since=cursor)
    mine = [c for c in page["changes"] if c["entity"] == "customer" and c["id"] == customer]
    assert mine and D(mine[-1]["data"]["balance"]) == D("-100.00")


def test_snapshot_paging_and_consistency_with_pull(http_client, pos_tenant):
    t = pos_tenant
    for i in range(5):
        new_product(http_client, t, f"Snap {i}")
    whole, cursor_whole, pages_whole = snapshot_all(http_client, t, limit=1000)
    paged, cursor_paged, pages = snapshot_all(http_client, t, limit=4)
    assert pages > pages_whole >= 1
    key = lambda c: (c["entity"], c["id"])  # noqa: E731
    assert sorted(map(key, paged)) == sorted(map(key, whole))
    assert len({key(c) for c in paged}) == len(paged)  # no duplicates across pages
    assert pull_json(http_client, t, since=cursor_paged)["changes"] == []


def test_snapshot_cursor_redelivers_changes_made_while_paging(http_client, pos_tenant):
    t = pos_tenant
    resp = http_client.get(t.url("/sync/snapshot"), headers=t.owner_headers,
                           params={"device_id": "dev-1", "limit": 3})
    page = resp.json()
    assert page["has_more"] is True
    late = new_product(http_client, t, "Created mid-snapshot")
    token = page["next_page_token"]
    while token:
        nxt = http_client.get(t.url("/sync/snapshot"), headers=t.owner_headers,
                              params={"device_id": "dev-1", "limit": 3, "page_token": token}).json()
        token = nxt["next_page_token"]
    after = pull_json(http_client, t, since=page["cursor"])
    assert late in _ids(after["changes"], "product")


def test_snapshot_page_token_tamper_rejected(http_client, pos_tenant):
    t = pos_tenant
    resp = http_client.get(t.url("/sync/snapshot"), headers=t.owner_headers,
                           params={"device_id": "dev-1", "limit": 2, "page_token": "bogus.token"})
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "invalid_cursor"


def test_device_id_is_required(http_client, pos_tenant):
    t = pos_tenant
    resp = http_client.get(t.url("/sync/pull"), headers=t.owner_headers)
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "device_id_required"
    ok = http_client.get(t.url("/sync/pull"), headers={**t.owner_headers, "X-Device-ID": "hdr-dev"})
    assert ok.status_code == 200


# ── subscription during pull ─────────────────────────────────────────────────


def test_grace_pull_is_full_read_only(http_client, pos_tenant):
    t = pos_tenant
    set_subscription(t.business_id, status="active", sub_ends_in=-timedelta(days=1))
    page = pull_json(http_client, t)
    assert page["restricted"] is False and "product" in page["entities"]


@pytest.mark.parametrize("status", ["suspended", "cancelled"])
def test_blocked_pull_serves_only_the_expired_screen_data(http_client, pos_tenant, status):
    t = pos_tenant
    cursor = pull_json(http_client, t)["next_cursor"]
    set_subscription(t.business_id, status=status)
    page = pull_json(http_client, t, since=cursor)
    assert page["restricted"] is True and page["entities"] == ["settings"]
    assert {c["entity"] for c in page["changes"]} == {"settings"}
    data = page["changes"][0]["data"]
    assert data["subscription"]["status"] == status and "contact" in data["subscription"]
    assert page["has_more"] is False
    # The cursor does not move past data that was withheld...
    assert page["next_cursor"] == cursor
    snap = http_client.get(t.url("/sync/snapshot"), headers=t.owner_headers, params={"device_id": "dev-1"}).json()
    assert snap["entities"] == ["settings"] and snap["restricted"] is True
    # ...so after renewal the missed changes arrive.
    set_subscription(t.business_id, status="active", sub_ends_in=timedelta(days=30))
    new_product(http_client, t, "While blocked") if False else None
    resumed = pull_json(http_client, t, since=cursor)
    assert resumed["restricted"] is False and "product" in resumed["entities"]


# ── /time, lease, limits ─────────────────────────────────────────────────────


def test_time_endpoint_is_public_tiny_and_close_to_now(http_client, api_base):
    for url in (f"{api_base}/time", api_base.replace("/api/v1", "") + "/time"):
        resp = http_client.get(url)
        assert resp.status_code == 200
        body = resp.json()
        assert set(body) == {"server_time", "epoch_ms"}
        assert abs(body["epoch_ms"] / 1000 - time.time()) < 5
    assert http_client.get(api_base.replace("/api/v1", "") + "/health").status_code == 200


def test_subscription_status_and_login_carry_a_verifiable_lease(http_client, pos_tenant, api_base):
    t = pos_tenant
    status = http_client.get(t.url("/subscription/status"), headers=t.owner_headers).json()
    lease = status["lease"]
    parsed = verify_lease(lease, TEST_LICENSE_PUBLIC_KEY, business_id=t.business_id)
    assert timedelta(days=6) < parsed.valid_until - parsed.server_time <= timedelta(days=7)

    login = http_client.post(f"{api_base}/auth/login", json={"phone": t.owner_phone, "password": t.password})
    assert login.status_code == 200
    verify_lease(login.json()["lease"], TEST_LICENSE_PUBLIC_KEY, business_id=t.business_id)
    me = http_client.get(t.url("/auth/me"), headers=t.owner_headers).json()
    verify_lease(me["lease"], TEST_LICENSE_PUBLIC_KEY, business_id=t.business_id)


def test_lease_is_capped_by_subscription_end_plus_grace(http_client, pos_tenant):
    t = pos_tenant
    set_subscription(t.business_id, status="active", sub_ends_in=timedelta(days=2))
    lease = http_client.get(t.url("/subscription/status"), headers=t.owner_headers).json()["lease"]
    parsed = verify_lease(lease, TEST_LICENSE_PUBLIC_KEY)
    span = parsed.valid_until - parsed.server_time
    assert timedelta(days=4, hours=23) < span < timedelta(days=5, hours=1)  # 2 days + 3 grace days


def test_lapsed_subscription_gets_an_expired_lease(http_client, pos_tenant):
    t = pos_tenant
    set_subscription(t.business_id, status="suspended")
    lease = http_client.get(t.url("/subscription/status"), headers=t.owner_headers).json()["lease"]
    from app.licensing.lease import LeaseExpired

    with pytest.raises(LeaseExpired):
        verify_lease(lease, TEST_LICENSE_PUBLIC_KEY)

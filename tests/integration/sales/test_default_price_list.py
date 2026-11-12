"""Default price list: seeded at registration, lazily recreated, backfilled, unique per business."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, text

from tests.helpers.db import db_scalar, sync_db_url
from tests.helpers.sqlexec import sql
from tests.helpers.tenants import create_product, create_unit, register_owner

pytestmark = pytest.mark.integration

MIGRATION = Path(__file__).resolve().parents[3] / "alembic" / "versions" / "020_default_price_list.py"


def _owner(client: httpx.Client, api_base: str, suffix: int):
    return register_owner(
        client, api_base, suffix,
        business_name=f"POSTest DefaultPL {suffix}", owner_name="Owner", phone_prefix="480",
    )


def _live_defaults(business_id: str) -> list[tuple]:
    return sql(
        "SELECT id, name, list_type::text, is_active, change_seq FROM price_lists "
        "WHERE business_id = :b AND is_default AND deleted_at IS NULL",
        b=business_id,
    )


def _remove_all_lists(business_id: str) -> None:
    sql("DELETE FROM price_lists WHERE business_id = :b", b=business_id)


def _run_migration_data_steps(extra_pre: str | None = None) -> None:
    """Execute the migration's upgrade() against the test DB (idempotent, only fills gaps)."""
    spec = importlib.util.spec_from_file_location("mig020", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    engine = create_engine(sync_db_url())
    try:
        with engine.begin() as conn:
            if extra_pre:
                conn.execute(text(extra_pre))

            class _Op:
                @staticmethod
                def execute(statement):
                    conn.execute(text(str(statement)))

            mod.op = _Op
            mod.upgrade()
    finally:
        engine.dispose()


def test_registration_creates_default_retail_list(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    rows = _live_defaults(t.business_id)
    assert len(rows) == 1
    _id, name, list_type, active, change_seq = rows[0]
    assert (name, list_type, active) == ("Retail", "retail", True)
    assert change_seq is not None, "default list must be stamped for the sync feed"

    resp = http_client.get(t.url("/prices/lists"), headers=t.owner_headers)
    assert resp.status_code == 200
    lists = resp.json()
    assert len(lists) == 1 and lists[0]["is_default"] is True and lists[0]["name"] == "Retail"


def test_set_price_works_on_fresh_business(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    create_unit(http_client, t)
    product_id = create_product(http_client, t)
    list_id = http_client.get(t.url("/prices/lists"), headers=t.owner_headers).json()[0]["id"]

    resp = http_client.post(
        t.url(f"/prices/lists/{list_id}/items"),
        headers=t.owner_headers,
        json={"product_id": product_id, "unit_price": "125.50"},
    )
    assert resp.status_code == 201, resp.text

    price = http_client.get(t.url(f"/prices/product/{product_id}"), headers=t.owner_headers)
    assert price.status_code == 200 and price.json()["unit_price"] in ("125.50", 125.5)

    # The item is stamped too, so it reaches devices via /sync/pull.
    assert db_scalar(
        "SELECT change_seq FROM price_list_items WHERE business_id = :b", {"b": t.business_id}
    ) is not None


def test_lazy_creation_when_default_missing(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    create_unit(http_client, t)
    product_id = create_product(http_client, t)

    # Listing recreates it.
    _remove_all_lists(t.business_id)
    lists = http_client.get(t.url("/prices/lists"), headers=t.owner_headers).json()
    assert len(lists) == 1 and lists[0]["is_default"] is True
    assert len(_live_defaults(t.business_id)) == 1

    # Reading a product price (the sale lookup path) recreates it too.
    _remove_all_lists(t.business_id)
    resp = http_client.get(t.url(f"/prices/product/{product_id}"), headers=t.owner_headers)
    assert resp.status_code == 200 and resp.json()["unit_price"] is None
    assert len(_live_defaults(t.business_id)) == 1

    # A soft-deleted "Retail" still holds the unique name: the new list gets another one.
    sql("UPDATE price_lists SET deleted_at = NOW(), is_default = FALSE WHERE business_id = :b", b=t.business_id)
    http_client.get(t.url("/prices/lists"), headers=t.owner_headers)
    rows = _live_defaults(t.business_id)
    assert len(rows) == 1 and rows[0][1] == "Retail Default"


def test_sale_price_lookup_recreates_default(http_client, api_base, unique_suffix):
    """The sync sale path always looks up catalog prices; a missing default must not fail it."""
    from tests.helpers.sync import op, push_ok, sale_payload
    from tests.helpers.tenants import build_pos_tenant

    t = build_pos_tenant(http_client, api_base, unique_suffix, phone_prefix="481")
    _remove_all_lists(t.business_id)
    res = push_ok(http_client, t, [op("sale.create", sale_payload(t))])[0]
    assert res["status"] == "applied", res
    assert len(_live_defaults(t.business_id)) == 1


def test_migration_backfill_is_idempotent_and_repairs_duplicates(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    bid = t.business_id

    # No lists at all -> created.
    _remove_all_lists(bid)
    _run_migration_data_steps()
    rows = _live_defaults(bid)
    assert len(rows) == 1 and rows[0][1] == "Retail" and rows[0][4] is not None  # stamped for sync

    # Re-run: nothing changes.
    first_id = rows[0][0]
    _run_migration_data_steps()
    assert [r[0] for r in _live_defaults(bid)] == [first_id]
    assert sql("SELECT count(*) FROM price_lists WHERE business_id = :b", b=bid)[0][0] == 1

    # Live active retail list that is not default -> promoted, not duplicated.
    sql("UPDATE price_lists SET is_default = FALSE WHERE business_id = :b", b=bid)
    _run_migration_data_steps()
    assert [r[0] for r in _live_defaults(bid)] == [first_id]
    assert sql("SELECT count(*) FROM price_lists WHERE business_id = :b", b=bid)[0][0] == 1

    # Pre-existing duplicate defaults (index absent) -> oldest kept, rest demoted, index rebuilt.
    _run_migration_data_steps(
        extra_pre=f"""
        DROP INDEX IF EXISTS uq_price_lists_one_default;
        INSERT INTO price_lists (id, business_id, name, list_type, is_default, is_active, created_at, updated_at)
        VALUES (gen_random_uuid(), '{bid}', 'Dup', 'retail', TRUE, TRUE, NOW() + interval '1 hour', NOW())
        """
    )
    assert [r[0] for r in _live_defaults(bid)] == [first_id]
    assert sql("SELECT count(*) FROM price_lists WHERE business_id = :b", b=bid)[0][0] == 2


def test_database_rejects_second_default(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    with pytest.raises(Exception, match="uq_price_lists_one_default"):
        sql(
            "INSERT INTO price_lists (id, business_id, name, list_type, is_default, is_active) "
            "VALUES (gen_random_uuid(), :b, 'Second', 'retail', TRUE, TRUE)",
            b=t.business_id,
        )
    # Creating a new default through the API demotes the old one instead of failing.
    resp = http_client.post(
        t.url("/prices/lists"), headers=t.owner_headers,
        json={"name": "Promo", "list_type": "retail", "is_default": True},
    )
    assert resp.status_code == 201, resp.text
    rows = _live_defaults(t.business_id)
    assert len(rows) == 1 and rows[0][1] == "Promo"


def test_no_duplicate_default_under_concurrency(http_client, api_base, unique_suffix):
    t = _owner(http_client, api_base, unique_suffix)
    create_unit(http_client, t)
    product_id = create_product(http_client, t)
    statuses: list[int] = []
    barrier = threading.Barrier(12)

    def worker(i: int) -> None:
        with httpx.Client(timeout=60.0) as c:
            barrier.wait()
            if i % 2:
                r = c.get(t.url("/prices/lists"), headers=t.owner_headers)
            else:
                r = c.get(t.url(f"/prices/product/{product_id}"), headers=t.owner_headers)
            statuses.append(r.status_code)

    for _ in range(3):  # several rounds from a clean slate
        statuses.clear()
        barrier.reset()
        _remove_all_lists(t.business_id)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        [th.start() for th in threads]
        [th.join() for th in threads]
        assert statuses == [200] * 12, statuses
        assert len(_live_defaults(t.business_id)) == 1
        assert sql("SELECT count(*) FROM price_lists WHERE business_id = :b", b=t.business_id)[0][0] == 1

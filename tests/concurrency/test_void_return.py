"""Concurrent void vs return race tests (migrated from test_void_return_concurrency)."""

from __future__ import annotations

import os
import threading
import uuid

import httpx
import pytest

from tests.helpers.db import db_scalar
from tests.helpers.tenants import TenantContext, build_void_race_tenant

pytestmark = [pytest.mark.integration, pytest.mark.concurrency, pytest.mark.slow]

ITERATIONS = int(os.environ.get("VOID_RETURN_ITERATIONS", "30"))


@pytest.fixture(scope="module")
def void_race_tenant(require_server, api_base: str) -> TenantContext:
    suffix = uuid.uuid4().int % 10_000_000
    with httpx.Client(timeout=120.0) as client:
        return build_void_race_tenant(client, api_base, suffix)


def _create_completed_sale(client: httpx.Client, tenant: TenantContext) -> dict[str, str]:
    assert tenant.manager_headers is not None
    assert tenant.product_id is not None
    assert tenant.shift_id is not None
    sale = client.post(
        tenant.url("/sales"),
        headers=tenant.manager_headers,
        json={
            "branch_id": tenant.branch_id,
            "register_shift_id": tenant.shift_id,
            "lines": [
                {
                    "product_id": tenant.product_id,
                    "qty": "2",
                    "unit_price": "50.00",
                }
            ],
            "payments": [{"payment_method": "cash", "amount": "100.00"}],
        },
    )
    assert sale.status_code == 201, sale.text
    body = sale.json()
    return {"sale_id": body["id"], "sale_line_id": body["lines"][0]["id"]}


def _sale_status(sale_id: str) -> str | None:
    return db_scalar(
        "SELECT status FROM sales WHERE id = :sid AND deleted_at IS NULL",
        {"sid": sale_id},
    )


def _return_line_count(sale_id: str) -> int:
    count = db_scalar(
        """
        SELECT COUNT(*)
        FROM sale_return_lines srl
        JOIN sale_returns sr ON sr.id = srl.sale_return_id
        WHERE sr.sale_id = :sid
          AND sr.deleted_at IS NULL
          AND srl.deleted_at IS NULL
        """,
        {"sid": sale_id},
    )
    return int(count or 0)


def _corruption_present(sale_id: str) -> bool:
    return _sale_status(sale_id) == "voided" and _return_line_count(sale_id) > 0


def _run_iteration(
    client: httpx.Client,
    tenant: TenantContext,
    iteration: int,
    stats: dict[str, int],
) -> None:
    sale_info = _create_completed_sale(client, tenant)
    sale_id = sale_info["sale_id"]
    sale_line_id = sale_info["sale_line_id"]
    assert tenant.manager_headers is not None

    barrier = threading.Barrier(2)
    outcomes: dict[str, httpx.Response] = {}

    def void_worker() -> None:
        barrier.wait()
        with httpx.Client(timeout=120.0) as thread_client:
            outcomes["void"] = thread_client.post(
                tenant.url(f"/sales/{sale_id}/void"),
                headers=tenant.manager_headers,
            )

    def return_worker() -> None:
        barrier.wait()
        with httpx.Client(timeout=120.0) as thread_client:
            outcomes["return"] = thread_client.post(
                tenant.url("/returns"),
                headers=tenant.manager_headers,
                json={
                    "branch_id": tenant.branch_id,
                    "sale_id": sale_id,
                    "lines": [
                        {
                            "product_id": tenant.product_id,
                            "sale_line_id": sale_line_id,
                            "qty": "1",
                            "unit_price": "50.00",
                        }
                    ],
                    "refund_payments": [{"payment_method": "cash", "amount": "50.00"}],
                },
            )

    t_void = threading.Thread(target=void_worker)
    t_return = threading.Thread(target=return_worker)
    t_void.start()
    t_return.start()
    t_void.join()
    t_return.join()

    void_resp = outcomes["void"]
    return_resp = outcomes["return"]
    void_ok = void_resp.status_code == 200
    return_ok = return_resp.status_code == 201

    assert void_resp.status_code < 500 and return_resp.status_code < 500, (
        f"iter {iteration}: void={void_resp.status_code} return={return_resp.status_code} "
        f"void_body={void_resp.text[:200]!r} return_body={return_resp.text[:200]!r}"
    )
    assert not (void_ok and return_ok), f"iter {iteration}: both void and return succeeded"
    assert not _corruption_present(sale_id), (
        f"iter {iteration}: corruption status={_sale_status(sale_id)!r} "
        f"return_lines={_return_line_count(sale_id)}"
    )

    if void_ok and not return_ok:
        stats["void_wins"] += 1
    elif return_ok and not void_ok:
        stats["return_wins"] += 1
    else:
        stats["other"] += 1


def test_void_return_race_invariants(
    void_race_tenant: TenantContext,
    api_base: str,
) -> None:
    stats = {"void_wins": 0, "return_wins": 0, "other": 0}
    with httpx.Client(timeout=120.0) as client:
        for i in range(1, ITERATIONS + 1):
            _run_iteration(client, void_race_tenant, i, stats)

    assert stats["void_wins"] + stats["return_wins"] + stats["other"] == ITERATIONS

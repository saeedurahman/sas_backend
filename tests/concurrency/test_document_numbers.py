"""Document number generation concurrency and correctness (migrated from script)."""

from __future__ import annotations

import threading
from datetime import date, datetime, timezone

import httpx
import pytest

from tests.helpers.db import db_execute, db_scalar
from tests.helpers.records import assert_ok

pytestmark = [pytest.mark.integration, pytest.mark.concurrency, pytest.mark.slow]


def _setup_tenant(client: httpx.Client, api_base: str, suffix: int) -> dict:
    password = "TestPass1"
    owner_phone = f"430{suffix:07d}"
    reg = client.post(
        f"{api_base}/auth/register",
        json={
            "business_name": f"DocNum {suffix}",
            "business_type_code": "retail",
            "owner_name": "DocNum Owner",
            "owner_phone": owner_phone,
            "owner_password": password,
            "branch_name": "Main",
        },
    )
    if reg.status_code != 201:
        raise RuntimeError(f"register failed: {reg.status_code} {reg.text}")
    body = reg.json()
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    branch_id = body["user"]["branch_id"]
    business_id = body["user"]["business_id"]

    unit = client.post(
        f"{api_base}/units",
        headers=headers,
        json={"name": "Piece", "symbol": "pc", "is_base_unit": True},
    )
    unit_id = unit.json()["id"]
    product = client.post(
        f"{api_base}/products",
        headers=headers,
        json={
            "name": f"Product {suffix}",
            "base_unit_id": unit_id,
            "product_type": "standard",
            "tracking_type": "none",
        },
    )
    product_id = product.json()["id"]
    supplier = client.post(
        f"{api_base}/suppliers",
        headers=headers,
        json={"name": f"Supplier {suffix}"},
    )
    supplier_id = supplier.json()["id"]

    client.post(
        f"{api_base}/adjustments",
        headers=headers,
        json={
            "branch_id": branch_id,
            "reason": "opening_balance",
            "lines": [
                {
                    "product_id": product_id,
                    "qty_delta": "100",
                    "cost_per_unit": "50.00",
                }
            ],
        },
    )

    return {
        "headers": headers,
        "branch_id": branch_id,
        "business_id": business_id,
        "product_id": product_id,
        "supplier_id": supplier_id,
    }


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _create_sale(client: httpx.Client, api_base: str, ctx: dict) -> httpx.Response:
    return client.post(
        f"{api_base}/sales",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "qty": "1",
                    "unit_price": "100.00",
                }
            ],
            "payments": [{"payment_method": "cash", "amount": "100.00"}],
        },
    )


def _create_po(client: httpx.Client, api_base: str, ctx: dict) -> httpx.Response:
    return client.post(
        f"{api_base}/purchases/orders",
        headers=ctx["headers"],
        json={
            "supplier_id": ctx["supplier_id"],
            "branch_id": ctx["branch_id"],
            "lines": [
                {
                    "product_id": ctx["product_id"],
                    "ordered_qty": "1",
                    "cost_per_unit": "50.00",
                }
            ],
        },
    )


def _ensure_expense_category(client: httpx.Client, api_base: str, ctx: dict) -> str:
    resp = client.post(
        f"{api_base}/expenses/categories",
        headers=ctx["headers"],
        json={"name": "Test Category", "code": "TEST"},
    )
    if resp.status_code != 201:
        raise RuntimeError(f"expense category failed: {resp.status_code}")
    return resp.json()["id"]


def _create_expense(
    client: httpx.Client,
    api_base: str,
    ctx: dict,
    category_id: str,
) -> httpx.Response:
    return client.post(
        f"{api_base}/expenses",
        headers=ctx["headers"],
        json={
            "branch_id": ctx["branch_id"],
            "expense_category_id": category_id,
            "expense_date": date.today().isoformat(),
            "amount": "25.00",
            "payments": [{"payment_method": "cash", "amount": "25.00"}],
        },
    )


def test_concurrent_sales_document_numbers(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix)
    date_key = _today_key()
    results: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        with httpx.Client(timeout=60.0) as thread_client:
            resp = _create_sale(thread_client, api_base, ctx)
            number = resp.json().get("sale_number") if resp.status_code == 201 else None
            with lock:
                results.append((resp.status_code, number))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [status for status, _ in results]
    numbers = [number for _, number in results if number]
    assert_ok(
        "concurrent sales all succeed",
        len(results) == 10 and all(status == 201 for status in statuses),
        f"statuses={statuses}",
    )
    assert_ok(
        "concurrent sales distinct numbers",
        len(numbers) == 10 and len(set(numbers)) == 10,
        f"numbers={sorted(numbers)}",
    )
    expected_prefix = f"INV-{date_key}-"
    assert_ok(
        "concurrent sales correct format",
        all(number.startswith(expected_prefix) for number in numbers),
        f"prefix={expected_prefix}",
    )


def test_concurrent_purchase_orders(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix + 1)
    date_key = _today_key()
    results: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        with httpx.Client(timeout=60.0) as thread_client:
            resp = _create_po(thread_client, api_base, ctx)
            number = resp.json().get("po_number") if resp.status_code == 201 else None
            with lock:
                results.append((resp.status_code, number))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [status for status, _ in results]
    numbers = [number for _, number in results if number]
    assert_ok(
        "concurrent POs all succeed",
        len(results) == 10 and all(status == 201 for status in statuses),
        f"statuses={statuses}",
    )
    assert_ok(
        "concurrent POs distinct numbers",
        len(numbers) == 10 and len(set(numbers)) == 10,
        f"numbers={sorted(numbers)}",
    )
    assert_ok(
        "concurrent POs correct format",
        all(number.startswith(f"PO-{date_key}-") for number in numbers),
        f"prefix=PO-{date_key}-",
    )


def test_concurrent_expenses(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix + 2)
    category_id = _ensure_expense_category(http_client, api_base, ctx)
    date_key = _today_key()
    results: list[tuple[int, str | None]] = []
    lock = threading.Lock()

    def worker() -> None:
        with httpx.Client(timeout=60.0) as thread_client:
            resp = _create_expense(thread_client, api_base, ctx, category_id)
            number = (
                resp.json().get("expense_number") if resp.status_code == 201 else None
            )
            with lock:
                results.append((resp.status_code, number))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    statuses = [status for status, _ in results]
    numbers = [number for _, number in results if number]
    assert_ok(
        "concurrent expenses all succeed",
        len(results) == 10 and all(status == 201 for status in statuses),
        f"statuses={statuses}",
    )
    assert_ok(
        "concurrent expenses distinct numbers",
        len(numbers) == 10 and len(set(numbers)) == 10,
        f"numbers={sorted(numbers)}",
    )
    assert_ok(
        "concurrent expenses correct format",
        all(number.startswith(f"EXP-{date_key}-") for number in numbers),
        f"prefix=EXP-{date_key}-",
    )


def test_sequential_sales(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix + 3)
    date_key = _today_key()
    numbers: list[str] = []
    for _ in range(5):
        resp = _create_sale(http_client, api_base, ctx)
        if resp.status_code != 201:
            assert_ok("sequential sales all succeed", False, str(resp.status_code))
            return
        numbers.append(resp.json()["sale_number"])

    expected = [f"INV-{date_key}-{i:04d}" for i in range(1, 6)]
    assert_ok(
        "sequential sales INV-0001..0005",
        numbers == expected,
        f"got={numbers}",
    )


def test_cross_day_reset(
    http_client: httpx.Client,
    api_base: str,
    unique_suffix: int,
) -> None:
    ctx = _setup_tenant(http_client, api_base, unique_suffix + 4)
    business_id = ctx["business_id"]
    date_key = _today_key()

    db_execute(
        """
        INSERT INTO document_number_counters (
            id, business_id, prefix, date_key, last_sequence
        ) VALUES (
            gen_random_uuid(), :business_id, 'INV', '19990101', 42
        )
        ON CONFLICT (business_id, prefix, date_key) DO UPDATE
        SET last_sequence = EXCLUDED.last_sequence
        """,
        {"business_id": business_id},
    )

    resp = _create_sale(http_client, api_base, ctx)
    if resp.status_code != 201:
        assert_ok("cross-day sale succeeds", False, str(resp.status_code))
        return

    sale_number = resp.json()["sale_number"]
    assert_ok(
        "cross-day starts at 0001 for today",
        sale_number == f"INV-{date_key}-0001",
        f"got={sale_number}",
    )

    counter = db_scalar(
        """
        SELECT last_sequence
        FROM document_number_counters
        WHERE business_id = :business_id
          AND prefix = 'INV'
          AND date_key = :date_key
        """,
        {"business_id": business_id, "date_key": date_key},
    )
    old_counter = db_scalar(
        """
        SELECT last_sequence
        FROM document_number_counters
        WHERE business_id = :business_id
          AND prefix = 'INV'
          AND date_key = '19990101'
        """,
        {"business_id": business_id},
    )
    assert_ok(
        "cross-day old date counter unchanged",
        int(old_counter or 0) == 42 and int(counter or 0) == 1,
        f"today={counter} old={old_counter}",
    )

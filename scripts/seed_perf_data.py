"""Seed high-volume test data for Phase 6a performance benchmarking.

Creates:
- 1 dedicated business ("Perf Benchmark Store", timezone "Asia/Karachi")
- 2 branches ("Main Branch", "East Branch")
- 1 owner user and 1 cashier user
- 1 default price list
- 20 product categories and 1 base unit ("Pcs")
- 5,000 products (with SKUs, costs, prices, stock levels)
- 5,000 barcodes
- 5,000 price list items
- 2,000 customers
- 50,000 sales spread over 12 months
- 150,000 sale lines (average 3 lines per sale)
- 50,000 sale payments
- ~500 expenses

REFUSES to run against DATABASE_URL (the dev DB). Requires TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
import random
import sys
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg2
from psycopg2.extras import execute_values

from tests.helpers.testdb import (
    TestDatabaseConfigError,
    development_database_url,
    require_test_database_url,
    sync_url,
)


def _check_database_safety() -> psycopg2.extensions.connection:
    test_url_str = require_test_database_url()
    dev_url_str = development_database_url()

    test_url = sync_url(test_url_str)
    if dev_url_str:
        dev_url = sync_url(dev_url_str)
        if (test_url.host or "localhost", test_url.port or 5432, test_url.database) == (
            dev_url.host or "localhost",
            dev_url.port or 5432,
            dev_url.database,
        ):
            print(
                f"SAFETY ERROR: TEST_DATABASE_URL points to the same DB as DATABASE_URL ({test_url.database}). Refusing to run!",
                file=sys.stderr,
            )
            sys.exit(1)

    print(f"Connecting to TEST database: {test_url.host or 'localhost'}:{test_url.port or 5432}/{test_url.database}")
    conn = psycopg2.connect(
        dbname=test_url.database,
        user=test_url.username,
        password=test_url.password,
        host=test_url.host,
        port=test_url.port,
    )
    conn.autocommit = False
    return conn


def seed():
    t_start = time.perf_counter()
    conn = _check_database_safety()
    cur = conn.cursor()

    try:
        # Check if perf business already exists, if so reuse or clean
        cur.execute("SELECT id FROM businesses WHERE name = 'Perf Benchmark Store'")
        row = cur.fetchone()
        if row:
            perf_b_id = row[0]
            cur.execute("SELECT count(*) FROM sales WHERE business_id = %s", (perf_b_id,))
            s_count = cur.fetchone()[0]
            if s_count >= 50000:
                print(f"Perf business already has {s_count} sales. Cleaning existing perf data to re-seed cleanly...")
                cur.execute("DELETE FROM businesses WHERE id = %s", (perf_b_id,))
                conn.commit()

        print("Seeding foundation: business_type, business, branches, users, categories, units...")
        # 1. business_type
        cur.execute("SELECT id FROM business_types LIMIT 1")
        b_type_row = cur.fetchone()
        if not b_type_row:
            b_type_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO business_types (id, name, code, is_active, created_at, updated_at) VALUES (%s, 'Retail', 'retail', true, now(), now())",
                (b_type_id,),
            )
        else:
            b_type_id = b_type_row[0]

        # 2. business
        business_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO businesses (
                id, name, legal_name, business_type_id, country_code, currency_code, timezone,
                subscription_plan, subscription_status, is_active, sync_status, created_at, updated_at
            ) VALUES (
                %s, 'Perf Benchmark Store', 'Perf Benchmark Store Pvt Ltd', %s, 'PK', 'PKR', 'Asia/Karachi',
                'pro', 'active', true, 'synced', now(), now()
            )
            """,
            (business_id, b_type_id),
        )

        # 3. business_config
        cur.execute(
            """
            INSERT INTO business_configs (
                id, business_id, allow_negative_stock, config_json, created_at, updated_at
            ) VALUES (%s, %s, true, '{}', now(), now())
            """,
            (str(uuid.uuid4()), business_id),
        )

        # 4. branches
        branch_1_id = str(uuid.uuid4())
        branch_2_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO branches (id, business_id, name, code, is_head_office, is_active, sync_status, created_at, updated_at)
            VALUES 
            (%s, %s, 'Main Branch', 'MAIN', true, true, 'synced', now(), now()),
            (%s, %s, 'East Branch', 'EAST', false, true, 'synced', now(), now())
            """,
            (branch_1_id, business_id, branch_2_id, business_id),
        )

        # 5. users (owner & cashier)
        from app.services.auth_service import hash_password
        pwd_hash = hash_password("TestPass1")
        owner_id = str(uuid.uuid4())
        cashier_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO users (
                id, business_id, default_branch_id, phone, full_name, password_hash, is_locked, failed_login_attempts,
                status, token_version, sync_status, created_at, updated_at
            ) VALUES
            (%s, %s, %s, '03009990001', 'Perf Owner', %s, false, 0, 'active', 0, 'synced', now(), now()),
            (%s, %s, %s, '03009990002', 'Perf Cashier', %s, false, 0, 'active', 0, 'synced', now(), now())
            """,
            (owner_id, business_id, branch_1_id, pwd_hash, cashier_id, business_id, branch_1_id, pwd_hash),
        )

        # Role assignments
        cur.execute("SELECT id, name FROM roles WHERE business_id = %s", (business_id,))
        roles = {r[1].lower(): r[0] for r in cur.fetchall()}
        if "owner" not in roles:
            # Seed owner role for this business
            owner_role_id = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO roles (id, business_id, name, is_system, created_at, updated_at) VALUES (%s, %s, 'Owner', true, now(), now())",
                (owner_role_id, business_id),
            )
            roles["owner"] = owner_role_id
            # Seed permissions
            cur.execute("SELECT id FROM permissions")
            perm_ids = [r[0] for r in cur.fetchall()]
            rp_rows = [(str(uuid.uuid4()), business_id, owner_role_id, pid, datetime.now(timezone.utc)) for pid in perm_ids]
            execute_values(cur, "INSERT INTO role_permissions (id, business_id, role_id, permission_id, created_at) VALUES %s", rp_rows)

        cur.execute(
            """
            INSERT INTO user_roles (id, business_id, user_id, role_id, created_at, updated_at)
            VALUES (%s, %s, %s, %s, now(), now())
            """,
            (str(uuid.uuid4()), business_id, owner_id, roles["owner"]),
        )

        # 6. unit
        unit_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO units (id, business_id, name, symbol, is_base_unit, created_at, updated_at) VALUES (%s, %s, 'Pieces', 'pcs', true, now(), now())",
            (unit_id, business_id),
        )

        # 7. categories (20 categories)
        cat_ids = [str(uuid.uuid4()) for _ in range(20)]
        cat_rows = [
            (cid, business_id, f"Category {i+1:02d}", f"cat-{i+1:02d}", i, True, datetime.now(timezone.utc), datetime.now(timezone.utc))
            for i, cid in enumerate(cat_ids)
        ]
        execute_values(cur, "INSERT INTO categories (id, business_id, name, slug, sort_order, is_active, created_at, updated_at) VALUES %s", cat_rows)

        # 8. default price list
        price_list_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO price_lists (id, business_id, name, list_type, is_default, is_active, sync_status, created_at, updated_at)
            VALUES (%s, %s, 'Standard Retail', 'retail', true, true, 'synced', now(), now())
            """,
            (price_list_id, business_id),
        )

        # 9. registers & shifts
        reg_1_id = str(uuid.uuid4())
        reg_2_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO cash_registers (id, business_id, branch_id, name, is_active, sync_status, created_at, updated_at)
            VALUES 
            (%s, %s, %s, 'Till 1', true, 'synced', now(), now()),
            (%s, %s, %s, 'Till 2', true, 'synced', now(), now())
            """,
            (reg_1_id, business_id, branch_1_id, reg_2_id, business_id, branch_2_id),
        )

        # Shifts across the year (24 shifts)
        shift_ids = [str(uuid.uuid4()) for _ in range(24)]
        tz = ZoneInfo("Asia/Karachi")
        now_dt = datetime.now(tz)
        shift_rows = []
        for i, sid in enumerate(shift_ids):
            s_open = now_dt - timedelta(days=365 - i * 15)
            s_close = s_open + timedelta(hours=14)
            shift_rows.append((
                sid, business_id, branch_1_id if i % 2 == 0 else branch_2_id,
                reg_1_id if i % 2 == 0 else reg_2_id,
                owner_id, owner_id, 'closed', 500.0, 15000.0, 15000.0, 0.0,
                s_open.astimezone(timezone.utc), s_close.astimezone(timezone.utc),
                'synced', s_open.astimezone(timezone.utc), s_close.astimezone(timezone.utc), owner_id
            ))
        execute_values(
            cur,
            """
            INSERT INTO register_shifts (
                id, business_id, branch_id, cash_register_id, opened_by, closed_by, status,
                opening_float, expected_cash, actual_cash, cash_difference, opened_at, closed_at,
                sync_status, created_at, updated_at, created_by
            ) VALUES %s
            """,
            shift_rows,
        )

        # 10. Customers: 2,000 customers
        print("Seeding 2,000 customers...")
        cust_ids = [str(uuid.uuid4()) for _ in range(2000)]
        cust_rows = [
            (
                cid, business_id, f"Customer {i+1:04d}", f"cust{i+1:04d}@example.com",
                f"0300{i+1:07d}", f"Address Line {i+1}", "Lahore" if i % 2 == 0 else "Karachi",
                50000.00, True, 'synced', datetime.now(timezone.utc), datetime.now(timezone.utc)
            )
            for i, cid in enumerate(cust_ids)
        ]
        execute_values(
            cur,
            """
            INSERT INTO customers (
                id, business_id, name, email, phone, address_line1, city,
                credit_limit, is_active, sync_status, created_at, updated_at
            ) VALUES %s
            """,
            cust_rows,
            page_size=2000,
        )

        # 11. Products: 5,000 products
        print("Seeding 5,000 products, barcodes, and price list items...")
        prod_ids = [str(uuid.uuid4()) for _ in range(5000)]
        prod_rows = []
        barcode_rows = []
        pli_rows = []
        stock_rows = []

        now_utc = datetime.now(timezone.utc)
        for i, pid in enumerate(prod_ids):
            sku = f"SKU-{i+1:05d}"
            name = f"Perf Product {i+1:05d}"
            cat_id = cat_ids[i % len(cat_ids)]
            cost = round(random.uniform(20.0, 500.0), 2)
            price = round(cost * 1.35, 2)
            
            prod_rows.append((
                pid, business_id, cat_id, unit_id, name, sku,
                'standard', 'none', True, True, True, 'synced',
                now_utc, now_utc, owner_id
            ))

            barcode_rows.append((
                str(uuid.uuid4()), business_id, pid, f"BAR{i+1:08d}", "code128", True,
                'synced', now_utc, now_utc
            ))

            pli_rows.append((
                str(uuid.uuid4()), business_id, price_list_id, pid, price, 1.0,
                'synced', now_utc, now_utc
            ))

            # Opening stock movement
            stock_rows.append((
                str(uuid.uuid4()), business_id, branch_1_id, pid, 'opening',
                100.0, cost, 'manual', pid, now_utc, 'synced', now_utc, now_utc, owner_id
            ))

        execute_values(
            cur,
            """
            INSERT INTO products (
                id, business_id, category_id, base_unit_id, name, sku,
                product_type, tracking_type, is_sellable, is_purchasable, is_active, sync_status,
                created_at, updated_at, created_by
            ) VALUES %s
            """,
            prod_rows,
            page_size=2500,
        )
        execute_values(
            cur,
            """
            INSERT INTO barcodes (
                id, business_id, product_id, barcode, barcode_type, is_primary,
                sync_status, created_at, updated_at
            ) VALUES %s
            """,
            barcode_rows,
            page_size=2500,
        )
        execute_values(
            cur,
            """
            INSERT INTO price_list_items (
                id, business_id, price_list_id, product_id, unit_price, min_qty,
                sync_status, created_at, updated_at
            ) VALUES %s
            """,
            pli_rows,
            page_size=2500,
        )
        execute_values(
            cur,
            """
            INSERT INTO stock_movements (
                id, business_id, branch_id, product_id, movement_type,
                qty, cost_per_unit, reference_type, reference_id, movement_at,
                sync_status, created_at, updated_at, created_by
            ) VALUES %s
            """,
            stock_rows,
            page_size=2500,
        )

        # 12. 50,000 Sales & 150,000 Sale Lines
        print("Generating 50,000 sales and 150,000 sale lines spread over 12 months...")
        sale_rows = []
        sale_line_rows = []
        payment_rows = []

        # Generate timestamps evenly distributed across 365 days
        start_date = datetime.now(tz) - timedelta(days=365)
        total_seconds = 365 * 86400

        statuses = ['completed'] * 96 + ['cancelled'] * 2 + ['voided'] * 2
        payment_methods = ['cash'] * 7 + ['card'] * 2 + ['bank_transfer'] * 1

        random.seed(42)

        for s_idx in range(50000):
            sale_id = str(uuid.uuid4())
            # Random time spread across 365 days, biased toward daytime hours (9 AM to 10 PM)
            day_offset = int((s_idx / 50000.0) * 365)
            hour = random.randint(9, 21)
            minute = random.randint(0, 59)
            second = random.randint(0, 59)
            sold_at_local = start_date + timedelta(days=day_offset, hours=hour, minutes=minute, seconds=second)
            sold_at_utc = sold_at_local.astimezone(timezone.utc)

            branch_id = branch_1_id if s_idx % 3 != 0 else branch_2_id
            shift_id = shift_ids[s_idx % len(shift_ids)]
            customer_id = cust_ids[random.randint(0, len(cust_ids) - 1)] if random.random() < 0.65 else None
            status = statuses[s_idx % len(statuses)]
            sale_number = f"INV-PERF-{s_idx+1:06d}"
            client_receipt_no = f"REC-PERF-{s_idx+1:06d}"

            # 3 lines per sale
            sale_subtotal = 0.0
            sale_tax = 0.0
            sale_discount = 0.0

            for line_idx in range(3):
                p_idx = (s_idx * 3 + line_idx) % len(prod_ids)
                pid = prod_ids[p_idx]
                qty = float(random.randint(1, 4))
                unit_price = round(float(random.randint(50, 400)), 2)
                discount = round(unit_price * qty * 0.05, 2) if random.random() < 0.2 else 0.0
                tax_rate = 0.16 if random.random() < 0.5 else 0.0
                tax_amount = round((unit_price * qty - discount) * tax_rate, 2)
                cost_per_unit = round(unit_price * 0.7, 2)

                sale_subtotal += unit_price * qty
                sale_discount += discount
                sale_tax += tax_amount

                sale_line_rows.append((
                    str(uuid.uuid4()), business_id, sale_id, pid,
                    qty, unit_price, 0.0, discount, tax_rate, tax_amount,
                    cost_per_unit, line_idx + 1, 'synced', sold_at_utc, sold_at_utc,
                    owner_id, False
                ))

            sale_total = round(sale_subtotal - sale_discount + sale_tax, 2)

            sale_rows.append((
                sale_id, business_id, branch_id, customer_id, shift_id, price_list_id,
                sale_number, 'pos', status, sold_at_utc, 'synced', sold_at_utc, sold_at_utc,
                owner_id, client_receipt_no, '{}', False
            ))

            if status == 'completed':
                pm = payment_methods[s_idx % len(payment_methods)]
                payment_rows.append((
                    str(uuid.uuid4()), business_id, sale_id, pm, sale_total,
                    'completed', f"REF-{s_idx+1:06d}", sold_at_utc, 'synced',
                    sold_at_utc, sold_at_utc, owner_id, sale_total, 0.0
                ))

        print("Inserting 50,000 sales...")
        execute_values(
            cur,
            """
            INSERT INTO sales (
                id, business_id, branch_id, customer_id, register_shift_id, price_list_id,
                sale_number, sale_type, status, sold_at, sync_status, created_at, updated_at,
                created_by, client_receipt_no, sync_flags, is_offline
            ) VALUES %s
            """,
            sale_rows,
            page_size=5000,
        )

        print("Inserting 150,000 sale lines...")
        execute_values(
            cur,
            """
            INSERT INTO sale_lines (
                id, business_id, sale_id, product_id,
                qty, unit_price, discount_pct, discount_amount, tax_rate, tax_amount,
                cost_per_unit, line_order, sync_status, created_at, updated_at,
                created_by, cost_estimated
            ) VALUES %s
            """,
            sale_line_rows,
            page_size=5000,
        )
        cur.execute(
            "UPDATE sale_lines SET net_amount = ROUND(qty * unit_price, 2) - discount_amount "
            "WHERE business_id = %s",
            (business_id,),
        )

        print("Inserting 50,000 sale payments...")
        execute_values(
            cur,
            """
            INSERT INTO sale_payments (
                id, business_id, sale_id, payment_method, amount,
                status, reference_no, paid_at, sync_status,
                created_at, updated_at, created_by, tendered_amount, change_given
            ) VALUES %s
            """,
            payment_rows,
            page_size=5000,
        )

        # 13. Expenses: 500 expenses
        print("Inserting 500 expenses...")
        exp_cat_id = str(uuid.uuid4())
        cur.execute(
            "INSERT INTO expense_categories (id, business_id, name, is_active, created_at, updated_at) VALUES (%s, %s, 'Utilities', true, now(), now())",
            (exp_cat_id, business_id),
        )
        expense_rows = []
        for e_idx in range(500):
            e_date = (start_date + timedelta(days=int((e_idx / 500.0) * 365))).date()
            e_amount = round(random.uniform(500.0, 10000.0), 2)
            expense_rows.append((
                str(uuid.uuid4()), business_id, branch_1_id if e_idx % 2 == 0 else branch_2_id,
                exp_cat_id, f"EXP-{e_idx+1:04d}", e_date, e_amount, 0.0,
                f"Monthly utility expense {e_idx+1}", now_utc, now_utc, owner_id
            ))
        execute_values(
            cur,
            """
            INSERT INTO expenses (
                id, business_id, branch_id, expense_category_id, expense_number,
                expense_date, amount, tax_amount, description, created_at, updated_at, created_by
            ) VALUES %s
            """,
            expense_rows,
            page_size=1000,
        )

        conn.commit()
        t_elapsed = time.perf_counter() - t_start
        print(f"\n[SUCCESS] Seeded performance data in {t_elapsed:.2f}s!")
        print(f"Business ID: {business_id}")
        print(f"Owner ID: {owner_id}")
        print(f"Branch IDs: {branch_1_id}, {branch_2_id}")

        # Run ANALYZE so PostgreSQL query planner has up-to-date statistics
        print("Running ANALYZE on sales, sale_lines, sale_payments, products, customers...")
        conn.autocommit = True
        cur.execute("ANALYZE sales; ANALYZE sale_lines; ANALYZE sale_payments; ANALYZE products; ANALYZE customers; ANALYZE stock_movements;")
        print("Statistics updated.")

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    seed()

"""
Default role → permission mapping and idempotent role_permissions seeding.

Single source of truth for owner / manager / cashier permission sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import Permission, Role, RolePermission

# All granular keys from seeds/002_full_permissions.sql (66 keys) plus
# seeds/003_discounts_view_permission.sql (discounts.view) plus
# seeds/004_restaurant_permissions.sql (10 restaurant.* keys).
RESTAURANT_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        "restaurant.floor_plans.view",
        "restaurant.floor_plans.manage",
        "restaurant.tables.view",
        "restaurant.tables.manage",
        "restaurant.tables.update_status",
        "restaurant.modifiers.view",
        "restaurant.modifiers.manage",
        "restaurant.kot.view",
        "restaurant.kot.update_status",
        "restaurant.kot.fire",
    }
)

RESTAURANT_CASHIER_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        "restaurant.floor_plans.view",
        "restaurant.tables.view",
        "restaurant.tables.update_status",
        "restaurant.modifiers.view",
        "restaurant.kot.fire",
    }
)

ALL_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        "auth.login",
        "auth.logout",
        "auth.refresh",
        "products.view",
        "products.create",
        "products.update",
        "products.delete",
        "products.manage_categories",
        "products.manage_brands",
        "products.manage_units",
        "products.manage_prices",
        "products.manage_barcodes",
        "inventory.view",
        "inventory.adjust",
        "inventory.purchase_orders.view",
        "inventory.purchase_orders.create",
        "inventory.purchase_orders.receive",
        "inventory.transfers.view",
        "inventory.transfers.create",
        "inventory.transfers.receive",
        "inventory.waste.view",
        "inventory.waste.create",
        "sales.view",
        "sales.create",
        "sales.cancel",
        "sales.apply_discount",
        "sales.override_price",
        "sales.returns.view",
        "sales.returns.create",
        "sales.payments.view",
        "discounts.view",
        "customers.view",
        "customers.create",
        "customers.update",
        "customers.ledger.view",
        "suppliers.view",
        "suppliers.create",
        "suppliers.update",
        "suppliers.ledger.view",
        "suppliers.ledger.payment",
        "expenses.view",
        "expenses.create",
        "expenses.update",
        "expenses.delete",
        "expenses.categories.manage",
        "registers.view",
        "registers.manage",
        "shifts.view",
        "shifts.open",
        "shifts.close",
        "shifts.cash_movement",
        "reports.view",
        "reports.sales",
        "reports.inventory",
        "reports.financial",
        "reports.analytics",
        "reports.export",
        "reports.fraud_alerts",
        "settings.view",
        "settings.manage",
        "users.view",
        "users.create",
        "users.update",
        "users.delete",
        "users.roles.manage",
        "notifications.view",
        "notifications.manage",
    }
) | RESTAURANT_PERMISSION_KEYS

# Legacy coarse key from seeds/001_permissions.sql — owner only.
OWNER_LEGACY_PERMISSION_KEYS: frozenset[str] = frozenset({"products.manage"})

MANAGER_EXCLUDED_KEYS: frozenset[str] = frozenset(
    {
        "users.roles.manage",
        "users.delete",
        "settings.manage",
        "reports.fraud_alerts",
        "expenses.delete",
        "products.delete",
        "registers.manage",
    }
)

CASHIER_PERMISSION_KEYS: frozenset[str] = frozenset(
    {
        "auth.login",
        "auth.logout",
        "auth.refresh",
        "products.view",
        "inventory.view",
        "sales.view",
        "sales.create",
        "sales.payments.view",
        "discounts.view",
        "customers.view",
        "customers.create",
        "registers.view",
        "shifts.open",
        "shifts.close",
        "shifts.cash_movement",
        "notifications.view",
    }
) | RESTAURANT_CASHIER_PERMISSION_KEYS

STANDARD_ROLE_NAMES: tuple[str, str, str] = ("owner", "manager", "cashier")

STANDARD_ROLE_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("owner", "Business owner"),
    ("manager", "Store manager"),
    ("cashier", "POS cashier"),
)


@dataclass(frozen=True)
class SeedStats:
    businesses_processed: int = 0
    roles_created: int = 0
    permissions_linked: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "businesses_processed": self.businesses_processed,
            "roles_created": self.roles_created,
            "permissions_linked": self.permissions_linked,
        }


def permission_keys_for_role(role_name: str) -> frozenset[str]:
    name = role_name.lower()
    if name == "owner":
        return ALL_PERMISSION_KEYS | OWNER_LEGACY_PERMISSION_KEYS
    if name == "manager":
        return ALL_PERMISSION_KEYS - MANAGER_EXCLUDED_KEYS
    if name == "cashier":
        return CASHIER_PERMISSION_KEYS
    raise ValueError(f"Not a standard role: {role_name!r}")


def _insert_role_permissions_sql() -> str:
    return """
        INSERT INTO role_permissions (id, business_id, role_id, permission_id, created_by)
        SELECT gen_random_uuid(), :business_id, :role_id, p.id, :created_by
        FROM permissions p
        WHERE p.permission_key = ANY(:permission_keys)
          AND NOT EXISTS (
              SELECT 1
              FROM role_permissions rp
              WHERE rp.business_id = :business_id
                AND rp.role_id = :role_id
                AND rp.permission_id = p.id
          )
    """


def _count_missing_role_permissions_sql() -> str:
    return """
        SELECT COUNT(*)
        FROM permissions p
        WHERE p.permission_key = ANY(:permission_keys)
          AND NOT EXISTS (
              SELECT 1
              FROM role_permissions rp
              WHERE rp.business_id = :business_id
                AND rp.role_id = :role_id
                AND rp.permission_id = p.id
          )
    """


def _count_available_permissions_sql() -> str:
    return """
        SELECT COUNT(*)
        FROM permissions p
        WHERE p.permission_key = ANY(:permission_keys)
    """


def seed_role_permissions_sync(
    connection: Connection,
    *,
    business_id: UUID,
    role_id: UUID,
    role_name: str,
    created_by: UUID | None = None,
    dry_run: bool = False,
) -> int:
    """Link permissions to a role. Returns number of rows inserted (or would insert)."""
    keys = sorted(permission_keys_for_role(role_name))
    params = {
        "business_id": business_id,
        "role_id": role_id,
        "permission_keys": keys,
        "created_by": created_by,
    }
    if dry_run:
        return connection.execute(
            text(_count_missing_role_permissions_sql()), params
        ).scalar_one()

    result = connection.execute(text(_insert_role_permissions_sql()), params)
    return result.rowcount


def _count_permissions_to_link_sync(
    connection: Connection,
    *,
    business_id: UUID,
    role_id: UUID | None,
    role_name: str,
    dry_run: bool,
) -> int:
    keys = sorted(permission_keys_for_role(role_name))
    if dry_run and role_id is None:
        return connection.execute(
            text(_count_available_permissions_sql()),
            {"permission_keys": keys},
        ).scalar_one()
    if role_id is None:
        return 0
    return seed_role_permissions_sync(
        connection,
        business_id=business_id,
        role_id=role_id,
        role_name=role_name,
        dry_run=dry_run,
    )


def _get_or_create_role_sync(
    connection: Connection,
    *,
    business_id: UUID,
    role_name: str,
    description: str,
    created_by: UUID | None = None,
    dry_run: bool = False,
) -> tuple[UUID | None, bool]:
    row = connection.execute(
        text(
            """
            SELECT id
            FROM roles
            WHERE business_id = :business_id
              AND lower(name) = lower(:role_name)
              AND deleted_at IS NULL
            LIMIT 1
            """
        ),
        {"business_id": business_id, "role_name": role_name},
    ).fetchone()

    if row is not None:
        return row[0], False

    if dry_run:
        return None, True

    role_id = connection.execute(
        text(
            """
            INSERT INTO roles (
                business_id, name, description, is_system, created_by
            ) VALUES (
                :business_id, :name, :description, TRUE, :created_by
            )
            RETURNING id
            """
        ),
        {
            "business_id": business_id,
            "name": role_name,
            "description": description,
            "created_by": created_by,
        },
    ).scalar_one()
    return role_id, True


def ensure_standard_roles_and_permissions_sync(
    connection: Connection,
    business_id: UUID,
    *,
    created_by: UUID | None = None,
    dry_run: bool = False,
) -> SeedStats:
    stats = SeedStats(businesses_processed=1)
    descriptions = dict(STANDARD_ROLE_DEFINITIONS)

    for role_name in STANDARD_ROLE_NAMES:
        role_id, created = _get_or_create_role_sync(
            connection,
            business_id=business_id,
            role_name=role_name,
            description=descriptions[role_name],
            created_by=created_by,
            dry_run=dry_run,
        )
        if created:
            stats = SeedStats(
                businesses_processed=stats.businesses_processed,
                roles_created=stats.roles_created + 1,
                permissions_linked=stats.permissions_linked,
            )

        if dry_run:
            linked = _count_permissions_to_link_sync(
                connection,
                business_id=business_id,
                role_id=role_id,
                role_name=role_name,
                dry_run=True,
            )
        elif role_id is not None:
            linked = seed_role_permissions_sync(
                connection,
                business_id=business_id,
                role_id=role_id,
                role_name=role_name,
                created_by=created_by,
                dry_run=False,
            )
        else:
            linked = 0
        stats = SeedStats(
            businesses_processed=stats.businesses_processed,
            roles_created=stats.roles_created,
            permissions_linked=stats.permissions_linked + linked,
        )

    return stats


def backfill_role_permissions_for_all_businesses(
    connection: Connection,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    rows = connection.execute(
        text("SELECT id FROM businesses WHERE deleted_at IS NULL ORDER BY created_at")
    ).fetchall()

    totals = SeedStats()
    per_business: list[dict[str, Any]] = []

    for (business_id,) in rows:
        stats = ensure_standard_roles_and_permissions_sync(
            connection,
            business_id,
            dry_run=dry_run,
        )
        per_business.append({"business_id": str(business_id), **stats.to_dict()})
        totals = SeedStats(
            businesses_processed=totals.businesses_processed + stats.businesses_processed,
            roles_created=totals.roles_created + stats.roles_created,
            permissions_linked=totals.permissions_linked + stats.permissions_linked,
        )

    return {"totals": totals.to_dict(), "businesses": per_business, "dry_run": dry_run}


async def _get_or_create_standard_role(
    db: AsyncSession,
    *,
    business_id: UUID,
    role_name: str,
    description: str,
    created_by: UUID | None,
) -> tuple[Role, bool]:
    result = await db.execute(
        select(Role).where(
            Role.business_id == business_id,
            func.lower(Role.name) == role_name.lower(),
            Role.deleted_at.is_(None),
        )
    )
    role = result.scalar_one_or_none()
    if role is not None:
        return role, False

    role = Role(
        business_id=business_id,
        name=role_name,
        description=description,
        is_system=True,
        created_by=created_by,
    )
    db.add(role)
    await db.flush()
    return role, True


async def seed_role_permissions_async(
    db: AsyncSession,
    *,
    business_id: UUID,
    role: Role,
    created_by: UUID | None = None,
) -> int:
    keys = permission_keys_for_role(role.name)
    result = await db.execute(
        select(Permission).where(Permission.permission_key.in_(keys))
    )
    permissions = list(result.scalars().all())
    if not permissions:
        return 0

    perm_ids = [p.id for p in permissions]
    existing = await db.execute(
        select(RolePermission.permission_id).where(
            RolePermission.business_id == business_id,
            RolePermission.role_id == role.id,
            RolePermission.permission_id.in_(perm_ids),
        )
    )
    existing_ids = set(existing.scalars().all())
    inserted = 0
    for permission in permissions:
        if permission.id in existing_ids:
            continue
        db.add(
            RolePermission(
                business_id=business_id,
                role_id=role.id,
                permission_id=permission.id,
                created_by=created_by,
            )
        )
        inserted += 1
    if inserted:
        await db.flush()
    return inserted


async def ensure_standard_roles_and_permissions(
    db: AsyncSession,
    business_id: UUID,
    *,
    created_by: UUID | None = None,
) -> SeedStats:
    """Create owner/manager/cashier roles if missing and seed role_permissions."""
    descriptions = dict(STANDARD_ROLE_DEFINITIONS)
    stats = SeedStats(businesses_processed=1)

    for role_name in STANDARD_ROLE_NAMES:
        role, created = await _get_or_create_standard_role(
            db,
            business_id=business_id,
            role_name=role_name,
            description=descriptions[role_name],
            created_by=created_by,
        )
        if created:
            stats = SeedStats(
                businesses_processed=stats.businesses_processed,
                roles_created=stats.roles_created + 1,
                permissions_linked=stats.permissions_linked,
            )
        linked = await seed_role_permissions_async(
            db,
            business_id=business_id,
            role=role,
            created_by=created_by,
        )
        stats = SeedStats(
            businesses_processed=stats.businesses_processed,
            roles_created=stats.roles_created,
            permissions_linked=stats.permissions_linked + linked,
        )

    return stats

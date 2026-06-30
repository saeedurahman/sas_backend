"""Floor plan and dine-in table management."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import SaleStatusEnum, SaleTypeEnum, TableStatusEnum
from app.models.restaurant import DiningTable, FloorPlan
from app.models.sales import Sale
from app.schemas.restaurant import (
    CreateDiningTableRequest,
    CreateFloorPlanRequest,
    DiningTableResponse,
    FloorLayoutResponse,
    FloorPlanWithTablesResponse,
    UpdateDiningTableRequest,
    UpdateFloorPlanRequest,
)
from app.services.stock_service import verify_branch

ACTIVE_TAB_STATUSES = (
    SaleStatusEnum.held.value,
    SaleStatusEnum.draft.value,
    SaleStatusEnum.partially_paid.value,
)

TABLE_STATUS_TRANSITIONS: dict[TableStatusEnum, frozenset[TableStatusEnum]] = {
    TableStatusEnum.available: frozenset(
        {TableStatusEnum.occupied, TableStatusEnum.reserved}
    ),
    TableStatusEnum.reserved: frozenset(
        {TableStatusEnum.occupied, TableStatusEnum.available}
    ),
    TableStatusEnum.occupied: frozenset(
        {TableStatusEnum.billing, TableStatusEnum.available}
    ),
    TableStatusEnum.billing: frozenset(
        {TableStatusEnum.cleaning, TableStatusEnum.occupied}
    ),
    TableStatusEnum.cleaning: frozenset({TableStatusEnum.available}),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_status_value(table: DiningTable) -> TableStatusEnum:
    return TableStatusEnum(table.status)


async def get_active_sale_ids_by_table_ids(
    db: AsyncSession,
    business_id: UUID,
    table_ids: list[UUID],
) -> dict[UUID, UUID]:
    """Map table_id → open dine-in tab sale_id (held/draft/partially_paid)."""
    if not table_ids:
        return {}

    result = await db.execute(
        select(Sale.table_id, Sale.id)
        .where(
            Sale.business_id == business_id,
            Sale.table_id.in_(table_ids),
            Sale.deleted_at.is_(None),
            Sale.sale_type == SaleTypeEnum.dine_in.value,
            Sale.status.in_(ACTIVE_TAB_STATUSES),
        )
        .order_by(Sale.sold_at.desc())
    )
    mapping: dict[UUID, UUID] = {}
    for table_id, sale_id in result.all():
        if table_id is not None and table_id not in mapping:
            mapping[table_id] = sale_id
    return mapping


def dining_table_to_response(
    table: DiningTable,
    *,
    active_sale_id: UUID | None = None,
) -> DiningTableResponse:
    return DiningTableResponse.model_validate(
        table,
        from_attributes=True,
    ).model_copy(update={"active_sale_id": active_sale_id})


def is_valid_table_status_transition(
    current: TableStatusEnum,
    target: TableStatusEnum,
    *,
    force: bool = False,
) -> bool:
    if force or current == target:
        return True
    return target in TABLE_STATUS_TRANSITIONS.get(current, frozenset())


async def transition_table_status(
    db: AsyncSession,
    table: DiningTable,
    new_status: TableStatusEnum,
    *,
    updated_by: UUID,
    force: bool = False,
    commit: bool = True,
) -> DiningTable:
    current = _table_status_value(table)
    if not is_valid_table_status_transition(current, new_status, force=force):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Invalid table status transition from '{current.value}' "
                f"to '{new_status.value}'"
            ),
        )

    now = _now()
    table.status = new_status.value
    table.updated_by = updated_by
    table.updated_at = now
    if commit:
        await db.commit()
        await db.refresh(table)
    else:
        await db.flush()
    return table


async def _verify_floor_plan(
    db: AsyncSession,
    floor_plan_id: UUID,
    business_id: UUID,
) -> FloorPlan:
    result = await db.execute(
        select(FloorPlan).where(
            FloorPlan.id == floor_plan_id,
            FloorPlan.business_id == business_id,
            FloorPlan.deleted_at.is_(None),
        )
    )
    floor_plan = result.scalar_one_or_none()
    if floor_plan is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Floor plan not found",
        )
    return floor_plan


async def get_floor_plans(
    db: AsyncSession,
    business_id: UUID,
    *,
    branch_id: UUID | None = None,
) -> list[FloorPlan]:
    stmt = (
        select(FloorPlan)
        .where(
            FloorPlan.business_id == business_id,
            FloorPlan.deleted_at.is_(None),
        )
        .order_by(FloorPlan.sort_order, FloorPlan.name)
    )
    if branch_id is not None:
        stmt = stmt.where(FloorPlan.branch_id == branch_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_floor_plan_by_id(
    db: AsyncSession,
    floor_plan_id: UUID,
    business_id: UUID,
) -> FloorPlan:
    return await _verify_floor_plan(db, floor_plan_id, business_id)


async def create_floor_plan(
    db: AsyncSession,
    business_id: UUID,
    data: CreateFloorPlanRequest,
    created_by: UUID,
) -> FloorPlan:
    await verify_branch(db, data.branch_id, business_id)
    now = _now()
    floor_plan = FloorPlan(
        business_id=business_id,
        branch_id=data.branch_id,
        name=data.name.strip(),
        sort_order=data.sort_order,
        layout_json=data.layout_json,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(floor_plan)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Floor plan name already exists for this branch",
        ) from exc
    await db.refresh(floor_plan)
    return floor_plan


async def update_floor_plan(
    db: AsyncSession,
    floor_plan_id: UUID,
    business_id: UUID,
    data: UpdateFloorPlanRequest,
    updated_by: UUID,
) -> FloorPlan:
    floor_plan = await _verify_floor_plan(db, floor_plan_id, business_id)
    now = _now()

    if data.name is not None:
        floor_plan.name = data.name.strip()
    if data.sort_order is not None:
        floor_plan.sort_order = data.sort_order
    if data.is_active is not None:
        floor_plan.is_active = data.is_active
    if data.layout_json is not None:
        floor_plan.layout_json = data.layout_json

    floor_plan.updated_by = updated_by
    floor_plan.updated_at = now
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Floor plan name already exists for this branch",
        ) from exc
    await db.refresh(floor_plan)
    return floor_plan


async def delete_floor_plan(
    db: AsyncSession,
    floor_plan_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    floor_plan = await _verify_floor_plan(db, floor_plan_id, business_id)
    now = _now()
    floor_plan.deleted_at = now
    floor_plan.deleted_by = deleted_by
    floor_plan.updated_at = now
    floor_plan.updated_by = deleted_by
    await db.commit()


async def get_dining_tables(
    db: AsyncSession,
    business_id: UUID,
    *,
    branch_id: UUID | None = None,
    floor_plan_id: UUID | None = None,
    table_status: TableStatusEnum | None = None,
) -> list[DiningTable]:
    stmt = (
        select(DiningTable)
        .where(
            DiningTable.business_id == business_id,
            DiningTable.deleted_at.is_(None),
        )
        .order_by(DiningTable.table_number)
    )
    if branch_id is not None:
        stmt = stmt.where(DiningTable.branch_id == branch_id)
    if floor_plan_id is not None:
        stmt = stmt.where(DiningTable.floor_plan_id == floor_plan_id)
    if table_status is not None:
        stmt = stmt.where(DiningTable.status == table_status.value)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_dining_table_responses(
    db: AsyncSession,
    business_id: UUID,
    *,
    branch_id: UUID | None = None,
    floor_plan_id: UUID | None = None,
    table_status: TableStatusEnum | None = None,
) -> list[DiningTableResponse]:
    tables = await get_dining_tables(
        db,
        business_id,
        branch_id=branch_id,
        floor_plan_id=floor_plan_id,
        table_status=table_status,
    )
    sale_by_table = await get_active_sale_ids_by_table_ids(
        db,
        business_id,
        [table.id for table in tables],
    )
    return [
        dining_table_to_response(
            table,
            active_sale_id=sale_by_table.get(table.id),
        )
        for table in tables
    ]


async def get_dining_table_by_id(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
) -> DiningTable:
    result = await db.execute(
        select(DiningTable).where(
            DiningTable.id == table_id,
            DiningTable.business_id == business_id,
            DiningTable.deleted_at.is_(None),
        )
    )
    table = result.scalar_one_or_none()
    if table is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Table not found",
        )
    return table


async def get_dining_table_response(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
) -> DiningTableResponse:
    table = await get_dining_table_by_id(db, table_id, business_id)
    sale_by_table = await get_active_sale_ids_by_table_ids(
        db,
        business_id,
        [table.id],
    )
    return dining_table_to_response(
        table,
        active_sale_id=sale_by_table.get(table.id),
    )


async def create_dining_table(
    db: AsyncSession,
    business_id: UUID,
    data: CreateDiningTableRequest,
    created_by: UUID,
) -> DiningTable:
    await verify_branch(db, data.branch_id, business_id)
    if data.floor_plan_id is not None:
        floor_plan = await _verify_floor_plan(db, data.floor_plan_id, business_id)
        if floor_plan.branch_id != data.branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Floor plan does not belong to the specified branch",
            )

    now = _now()
    table = DiningTable(
        business_id=business_id,
        branch_id=data.branch_id,
        floor_plan_id=data.floor_plan_id,
        table_number=data.table_number,
        capacity=data.capacity,
        status=TableStatusEnum.available.value,
        pos_x=data.pos_x,
        pos_y=data.pos_y,
        is_active=True,
        created_by=created_by,
        created_at=now,
        updated_at=now,
    )
    db.add(table)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Table number already exists for this branch",
        ) from exc
    await db.refresh(table)
    return table


async def update_dining_table(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
    data: UpdateDiningTableRequest,
    updated_by: UUID,
) -> DiningTable:
    table = await get_dining_table_by_id(db, table_id, business_id)
    now = _now()

    if data.floor_plan_id is not None:
        floor_plan = await _verify_floor_plan(db, data.floor_plan_id, business_id)
        if floor_plan.branch_id != table.branch_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Floor plan does not belong to the table branch",
            )
        table.floor_plan_id = data.floor_plan_id
    if data.table_number is not None:
        table.table_number = data.table_number
    if data.capacity is not None:
        table.capacity = data.capacity
    if data.pos_x is not None:
        table.pos_x = data.pos_x
    if data.pos_y is not None:
        table.pos_y = data.pos_y
    if data.is_active is not None:
        table.is_active = data.is_active

    table.updated_by = updated_by
    table.updated_at = now
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Table number already exists for this branch",
        ) from exc
    await db.refresh(table)
    return table


async def delete_dining_table(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
    deleted_by: UUID,
) -> None:
    table = await get_dining_table_by_id(db, table_id, business_id)
    now = _now()
    table.deleted_at = now
    table.deleted_by = deleted_by
    table.updated_at = now
    table.updated_by = deleted_by
    await db.commit()


async def update_dining_table_status(
    db: AsyncSession,
    table_id: UUID,
    business_id: UUID,
    new_status: TableStatusEnum,
    updated_by: UUID,
    *,
    force: bool = False,
) -> DiningTable:
    table = await get_dining_table_by_id(db, table_id, business_id)
    return await transition_table_status(
        db,
        table,
        new_status,
        updated_by=updated_by,
        force=force,
    )


async def get_floor_layout(
    db: AsyncSession,
    business_id: UUID,
    branch_id: UUID,
) -> FloorLayoutResponse:
    await verify_branch(db, branch_id, business_id)

    floor_plans_result = await db.execute(
        select(FloorPlan)
        .where(
            FloorPlan.business_id == business_id,
            FloorPlan.branch_id == branch_id,
            FloorPlan.deleted_at.is_(None),
        )
        .options(selectinload(FloorPlan.tables))
        .order_by(FloorPlan.sort_order, FloorPlan.name)
    )
    floor_plans = list(floor_plans_result.scalars().unique().all())

    tables_result = await db.execute(
        select(DiningTable)
        .where(
            DiningTable.business_id == business_id,
            DiningTable.branch_id == branch_id,
            DiningTable.deleted_at.is_(None),
        )
        .order_by(DiningTable.table_number)
    )
    all_tables = list(tables_result.scalars().all())
    sale_by_table = await get_active_sale_ids_by_table_ids(
        db,
        business_id,
        [table.id for table in all_tables],
    )
    tables_by_plan: dict[UUID, list[DiningTable]] = {}
    unassigned: list[DiningTable] = []

    for table in all_tables:
        if table.floor_plan_id is None:
            unassigned.append(table)
            continue
        tables_by_plan.setdefault(table.floor_plan_id, []).append(table)

    nested_plans: list[FloorPlanWithTablesResponse] = []
    for floor_plan in floor_plans:
        active_tables = [
            table
            for table in tables_by_plan.get(floor_plan.id, [])
            if table.deleted_at is None
        ]
        plan_data = FloorPlanWithTablesResponse.model_validate(
            floor_plan,
            from_attributes=True,
        )
        nested_plans.append(
            plan_data.model_copy(
                update={
                    "tables": [
                        dining_table_to_response(
                            table,
                            active_sale_id=sale_by_table.get(table.id),
                        )
                        for table in active_tables
                    ]
                }
            )
        )

    return FloorLayoutResponse(
        branch_id=branch_id,
        floor_plans=nested_plans,
        unassigned_tables=[
            dining_table_to_response(
                table,
                active_sale_id=sale_by_table.get(table.id),
            )
            for table in unassigned
        ],
    )

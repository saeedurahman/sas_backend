from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.inventory import (
    CreateSupplierRequest,
    SupplierResponse,
    UpdateSupplierRequest,
)
from app.services.supplier_service import (
    create_supplier,
    delete_supplier,
    get_supplier_by_id,
    get_suppliers,
    update_supplier,
)

router = APIRouter(prefix="/suppliers", tags=["Suppliers"])


@router.get("", response_model=list[SupplierResponse], status_code=status.HTTP_200_OK)
async def list_suppliers(
    search: str | None = Query(default=None),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_suppliers(db, current_user.business_id, search)


@router.post("", response_model=SupplierResponse, status_code=status.HTTP_201_CREATED)
async def create_supplier_endpoint(
    data: CreateSupplierRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_supplier(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{supplier_id}",
    response_model=SupplierResponse,
    status_code=status.HTTP_200_OK,
)
async def get_supplier(
    supplier_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_supplier_by_id(db, supplier_id, current_user.business_id)


@router.put(
    "/{supplier_id}",
    response_model=SupplierResponse,
    status_code=status.HTTP_200_OK,
)
async def update_supplier_endpoint(
    supplier_id: UUID,
    data: UpdateSupplierRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_supplier(
        db, supplier_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{supplier_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_supplier_endpoint(
    supplier_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    await delete_supplier(
        db, supplier_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="Supplier deleted")

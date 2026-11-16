from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status

from app.branch_scope import assert_branch_access, scoped_branch_query
from app.core.idempotency import run_idempotent
from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.expense import (
    SupplierBalanceResponse,
    SupplierLedgerListResponse,
    SupplierLedgerResponse,
    SupplierPaymentRequest,
)
from app.services.supplier_ledger_service import (
    get_supplier_balance,
    get_supplier_ledger,
    record_supplier_payment,
)

router = APIRouter(prefix="/supplier-ledger", tags=["Supplier Ledger"])


@router.get(
    "/{supplier_id}/balance",
    response_model=SupplierBalanceResponse,
    status_code=status.HTTP_200_OK,
)
async def supplier_balance(
    supplier_id: UUID,
    branch_id: UUID | None = Depends(scoped_branch_query),
    current_user: User = Depends(require_permission("suppliers.ledger.view")),
    db=Depends(get_db),
):
    balance = await get_supplier_balance(
        db, supplier_id, current_user.business_id, branch_id=branch_id
    )
    return SupplierBalanceResponse(supplier_id=supplier_id, balance=balance)


@router.get(
    "/{supplier_id}",
    response_model=SupplierLedgerListResponse,
    status_code=status.HTTP_200_OK,
)
async def list_supplier_ledger(
    supplier_id: UUID,
    branch_id: UUID | None = Depends(scoped_branch_query),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("suppliers.ledger.view")),
    db=Depends(get_db),
):
    entries, total = await get_supplier_ledger(
        db, supplier_id, current_user.business_id, skip, limit, branch_id=branch_id
    )
    return SupplierLedgerListResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[SupplierLedgerResponse.model_validate(e) for e in entries],
    )


@router.post(
    "/{supplier_id}/payment",
    response_model=SupplierLedgerResponse,
    status_code=status.HTTP_201_CREATED,
)
async def supplier_payment(
    request: Request,
    supplier_id: UUID,
    data: SupplierPaymentRequest,
    current_user: User = Depends(require_permission("suppliers.ledger.payment")),
    db=Depends(get_db),
):
    assert_branch_access(current_user, data.branch_id)
    return await run_idempotent(
        request,
        business_id=current_user.business_id,
        user_id=current_user.id,
        body=data,
        response_model=SupplierLedgerResponse,
        success_status=201,
        handler=lambda: record_supplier_payment(
            db, current_user.business_id, supplier_id, data, current_user.id
        ),
    )


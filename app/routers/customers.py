from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.sales import (
    CreateCustomerPaymentRequest,
    CreateCustomerRequest,
    CustomerLedgerResponse,
    CustomerPaymentResponse,
    CustomerResponse,
    UpdateCustomerRequest,
)
from app.services.customer_service import (
    create_customer,
    get_customer_balance,
    get_customer_by_id,
    get_customer_ledger,
    get_customers,
    record_customer_payment,
    update_customer,
)

router = APIRouter(prefix="/customers", tags=["Customers"])


class CustomerBalanceResponse(BaseModel):
    customer_id: UUID
    balance: Decimal


@router.get("", response_model=list[CustomerResponse], status_code=status.HTTP_200_OK)
async def list_customers(
    search: str | None = Query(default=None),
    current_user: User = Depends(require_permission("customers.view")),
    db=Depends(get_db),
):
    return await get_customers(db, current_user.business_id, search)


@router.post("", response_model=CustomerResponse, status_code=status.HTTP_201_CREATED)
async def create_customer_endpoint(
    data: CreateCustomerRequest,
    current_user: User = Depends(require_permission("customers.create")),
    db=Depends(get_db),
):
    return await create_customer(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{customer_id}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
)
async def get_customer(
    customer_id: UUID,
    current_user: User = Depends(require_permission("customers.view")),
    db=Depends(get_db),
):
    return await get_customer_by_id(db, customer_id, current_user.business_id)


@router.put(
    "/{customer_id}",
    response_model=CustomerResponse,
    status_code=status.HTTP_200_OK,
)
async def update_customer_endpoint(
    customer_id: UUID,
    data: UpdateCustomerRequest,
    current_user: User = Depends(require_permission("customers.update")),
    db=Depends(get_db),
):
    return await update_customer(
        db, customer_id, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{customer_id}/balance",
    response_model=CustomerBalanceResponse,
    status_code=status.HTTP_200_OK,
)
async def get_balance(
    customer_id: UUID,
    current_user: User = Depends(require_permission("customers.ledger.view")),
    db=Depends(get_db),
):
    balance = await get_customer_balance(
        db, customer_id, current_user.business_id
    )
    return CustomerBalanceResponse(customer_id=customer_id, balance=balance)


@router.get(
    "/{customer_id}/ledger",
    response_model=list[CustomerLedgerResponse],
    status_code=status.HTTP_200_OK,
)
async def get_ledger(
    customer_id: UUID,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("customers.ledger.view")),
    db=Depends(get_db),
):
    entries, _ = await get_customer_ledger(
        db, customer_id, current_user.business_id, skip, limit
    )
    return entries


@router.post(
    "/{customer_id}/payments",
    response_model=CustomerPaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def record_customer_payment_endpoint(
    customer_id: UUID,
    data: CreateCustomerPaymentRequest,
    current_user: User = Depends(require_permission("customers.update")),
    db=Depends(get_db),
):
    return await record_customer_payment(
        db,
        current_user.business_id,
        customer_id,
        data,
        current_user.id,
    )

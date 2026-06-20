from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.auth import MessageResponse
from app.schemas.expense import (
    CreateExpenseCategoryRequest,
    CreateExpensePaymentRequest,
    CreateExpenseRequest,
    ExpenseCategoryResponse,
    ExpenseListResponse,
    ExpensePaymentResponse,
    ExpenseResponse,
    PaginatedExpenseResponse,
    UpdateExpenseCategoryRequest,
    UpdateExpenseRequest,
)
from app.services.expense_service import (
    add_expense_payment,
    create_expense,
    create_expense_category,
    delete_expense,
    delete_expense_category,
    get_expense_by_id,
    get_expense_categories,
    get_expense_category_by_id,
    get_expenses,
    update_expense,
    update_expense_category,
)

router = APIRouter(prefix="/expenses", tags=["Expenses"])


@router.get(
    "/categories",
    response_model=list[ExpenseCategoryResponse],
    status_code=status.HTTP_200_OK,
)
async def list_expense_categories(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_expense_categories(db, current_user.business_id)


@router.post(
    "/categories",
    response_model=ExpenseCategoryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_category(
    data: CreateExpenseCategoryRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_expense_category(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/categories/{category_id}",
    response_model=ExpenseCategoryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_category(
    category_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_expense_category_by_id(
        db, category_id, current_user.business_id
    )


@router.put(
    "/categories/{category_id}",
    response_model=ExpenseCategoryResponse,
    status_code=status.HTTP_200_OK,
)
async def update_category(
    category_id: UUID,
    data: UpdateExpenseCategoryRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_expense_category(
        db, category_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/categories/{category_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_category(
    category_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    await delete_expense_category(
        db, category_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="Expense category deleted")


@router.get("", response_model=PaginatedExpenseResponse, status_code=status.HTTP_200_OK)
async def list_expenses(
    branch_id: UUID | None = Query(default=None),
    category_id: UUID | None = Query(default=None),
    supplier_id: UUID | None = Query(default=None),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    items, total = await get_expenses(
        db,
        current_user.business_id,
        branch_id,
        category_id,
        supplier_id,
        date_from,
        date_to,
        skip,
        limit,
    )
    return PaginatedExpenseResponse(
        total=total,
        skip=skip,
        limit=limit,
        items=[ExpenseListResponse.model_validate(e) for e in items],
    )


@router.post("", response_model=ExpenseResponse, status_code=status.HTTP_201_CREATED)
async def create_expense_endpoint(
    data: CreateExpenseRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await create_expense(
        db, current_user.business_id, data, current_user.id
    )


@router.post(
    "/{expense_id}/payments",
    response_model=ExpensePaymentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_expense_payment_endpoint(
    expense_id: UUID,
    data: CreateExpensePaymentRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await add_expense_payment(
        db,
        current_user.business_id,
        expense_id,
        data,
        current_user.id,
    )


@router.get(
    "/{expense_id}",
    response_model=ExpenseResponse,
    status_code=status.HTTP_200_OK,
)
async def get_expense(
    expense_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_expense_by_id(db, expense_id, current_user.business_id)


@router.put(
    "/{expense_id}",
    response_model=ExpenseResponse,
    status_code=status.HTTP_200_OK,
)
async def update_expense_endpoint(
    expense_id: UUID,
    data: UpdateExpenseRequest,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    return await update_expense(
        db, expense_id, current_user.business_id, data, current_user.id
    )


@router.delete(
    "/{expense_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_expense_endpoint(
    expense_id: UUID,
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    await delete_expense(
        db, expense_id, current_user.business_id, current_user.id
    )
    return MessageResponse(message="Expense deleted")

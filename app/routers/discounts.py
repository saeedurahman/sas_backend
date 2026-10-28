from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel

from app.database import get_db
from app.dependencies import require_owner, require_permission
from app.models.user import User
from app.schemas.sales import CreateDiscountSchemeRequest, DiscountSchemeResponse
from app.services.discount_service import (
    create_discount_scheme,
    get_discount_scheme_by_id,
    get_discount_schemes,
    update_discount_scheme,
)

router = APIRouter(prefix="/discounts", tags=["Discounts"])


class UpdateDiscountSchemeBody(BaseModel):
    name: str | None = None
    discount_value: Decimal | None = None
    min_purchase_amount: Decimal | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    is_active: bool | None = None
    applies_to_json: dict[str, Any] | None = None


@router.get(
    "",
    response_model=list[DiscountSchemeResponse],
    status_code=status.HTTP_200_OK,
)
async def list_discounts(
    current_user: User = Depends(require_permission("discounts.view")),
    db=Depends(get_db),
):
    return await get_discount_schemes(db, current_user.business_id)


@router.post(
    "",
    response_model=DiscountSchemeResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_discount(
    data: CreateDiscountSchemeRequest,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await create_discount_scheme(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{scheme_id}",
    response_model=DiscountSchemeResponse,
    status_code=status.HTTP_200_OK,
)
async def get_discount(
    scheme_id: UUID,
    current_user: User = Depends(require_permission("discounts.view")),
    db=Depends(get_db),
):
    return await get_discount_scheme_by_id(
        db, scheme_id, current_user.business_id
    )


@router.put(
    "/{scheme_id}",
    response_model=DiscountSchemeResponse,
    status_code=status.HTTP_200_OK,
)
async def update_discount(
    scheme_id: UUID,
    data: UpdateDiscountSchemeBody,
    current_user: User = Depends(require_owner),
    db=Depends(get_db),
):
    return await update_discount_scheme(
        db,
        scheme_id,
        current_user.business_id,
        current_user.id,
        name=data.name,
        discount_value=data.discount_value,
        min_purchase_amount=data.min_purchase_amount,
        valid_from=data.valid_from,
        valid_to=data.valid_to,
        is_active=data.is_active,
        applies_to_json=data.applies_to_json,
    )

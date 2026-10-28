from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.database import get_db
from app.dependencies import require_permission
from app.models.user import User
from app.schemas.sales import (
    CreateTaxRateRequest,
    TaxRateResponse,
    UpdateTaxRateRequest,
)
from app.services.tax_service import (
    create_tax_rate,
    get_tax_rate_by_id,
    get_tax_rates,
    update_tax_rate,
)

router = APIRouter(prefix="/tax-rates", tags=["Tax Rates"])


@router.get("", response_model=list[TaxRateResponse], status_code=status.HTTP_200_OK)
async def list_tax_rates(
    current_user: User = Depends(require_permission("settings.view")),
    db=Depends(get_db),
):
    return await get_tax_rates(db, current_user.business_id)


@router.post("", response_model=TaxRateResponse, status_code=status.HTTP_201_CREATED)
async def create_tax_rate_endpoint(
    data: CreateTaxRateRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    return await create_tax_rate(
        db, current_user.business_id, data, current_user.id
    )


@router.get(
    "/{tax_rate_id}",
    response_model=TaxRateResponse,
    status_code=status.HTTP_200_OK,
)
async def get_tax_rate(
    tax_rate_id: UUID,
    current_user: User = Depends(require_permission("settings.view")),
    db=Depends(get_db),
):
    return await get_tax_rate_by_id(
        db, tax_rate_id, current_user.business_id
    )


@router.put(
    "/{tax_rate_id}",
    response_model=TaxRateResponse,
    status_code=status.HTTP_200_OK,
)
async def update_tax_rate_endpoint(
    tax_rate_id: UUID,
    data: UpdateTaxRateRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    return await update_tax_rate(
        db, tax_rate_id, current_user.business_id, data, current_user.id
    )

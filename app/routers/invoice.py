import io
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.database import get_db
from app.dependencies import get_current_user, require_manager
from app.models.user import User
from app.schemas.invoice import InvoiceData, ThermalReceiptData
from app.services.export_service import (
    export_inventory_csv,
    export_inventory_excel,
    export_sales_csv,
    export_sales_excel,
)
from app.services.invoice_service import (
    get_invoice_data,
    get_thermal_receipt_data,
)

router = APIRouter(prefix="/invoice", tags=["Invoice & Export"])

_EXCEL_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


@router.get(
    "/export/sales",
    status_code=status.HTTP_200_OK,
)
async def export_sales(
    date_from: date = Query(...),
    date_to: date = Query(...),
    branch_id: UUID | None = Query(default=None),
    format: str = Query(default="csv"),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    if format not in ("csv", "excel"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format must be 'csv' or 'excel'",
        )

    date_label = f"{date_from}_{date_to}"
    if format == "csv":
        data = await export_sales_csv(
            db,
            current_user.business_id,
            date_from,
            date_to,
            branch_id,
        )
        return StreamingResponse(
            iter([data]),
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="sales_{date_label}.csv"'
            },
        )

    data = await export_sales_excel(
        db,
        current_user.business_id,
        date_from,
        date_to,
        branch_id,
    )
    return StreamingResponse(
        io.BytesIO(data),
        media_type=_EXCEL_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="sales_{date_label}.xlsx"'
        },
    )


@router.get(
    "/export/inventory",
    status_code=status.HTTP_200_OK,
)
async def export_inventory(
    branch_id: UUID | None = Query(default=None),
    format: str = Query(default="csv"),
    current_user: User = Depends(require_manager),
    db=Depends(get_db),
):
    if format not in ("csv", "excel"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="format must be 'csv' or 'excel'",
        )

    if format == "csv":
        data = await export_inventory_csv(
            db, current_user.business_id, branch_id
        )
        return StreamingResponse(
            iter([data]),
            media_type="text/csv",
            headers={
                "Content-Disposition": 'attachment; filename="inventory.csv"'
            },
        )

    data = await export_inventory_excel(
        db, current_user.business_id, branch_id
    )
    return StreamingResponse(
        io.BytesIO(data),
        media_type=_EXCEL_MEDIA_TYPE,
        headers={
            "Content-Disposition": 'attachment; filename="inventory.xlsx"'
        },
    )


@router.get(
    "/{sale_id}",
    response_model=InvoiceData,
    status_code=status.HTTP_200_OK,
)
async def invoice_data(
    sale_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_invoice_data(
        db, sale_id, current_user.business_id
    )


@router.get(
    "/{sale_id}/thermal",
    response_model=ThermalReceiptData,
    status_code=status.HTTP_200_OK,
)
async def thermal_receipt(
    sale_id: UUID,
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    return await get_thermal_receipt_data(
        db, sale_id, current_user.business_id
    )

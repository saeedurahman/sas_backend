import io
from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, PlainTextResponse, Response, StreamingResponse

from app.branch_scope import scoped_branch_query
from app.database import get_db
from app.dependencies import apply_business_timezone, require_permission
from app.models.user import User
from app.schemas.invoice import InvoiceData, ThermalReceiptData
from app.services.export_service import (
    export_inventory_csv,
    export_inventory_excel,
    export_sales_csv,
    export_sales_excel,
)
from app.services.business_time import get_business_tz
from app.services.invoice_service import (
    get_invoice_data,
    get_thermal_receipt_data,
)
from app.services.receipt_layout_service import resolve_layout
from app.services.receipt_link_service import link_url_for_sale
from app.services.receipt_render import (
    build_receipt_model,
    render_html,
    render_pdf,
    render_text,
)

router = APIRouter(
    prefix="/invoice",
    tags=["Invoice & Export"],
    dependencies=[Depends(apply_business_timezone)],
)

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
    branch_id: UUID | None = Depends(scoped_branch_query),
    format: str = Query(default="csv"),
    current_user: User = Depends(require_permission("reports.export")),
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
    branch_id: UUID | None = Depends(scoped_branch_query),
    format: str = Query(default="csv"),
    current_user: User = Depends(require_permission("reports.export")),
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
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    return await get_invoice_data(
        db, sale_id, current_user.business_id
    )


async def _receipt_model(db, current_user, sale_id: UUID, channel: str, request: Request | None):
    invoice = await get_invoice_data(db, sale_id, current_user.business_id)
    layout = await resolve_layout(db, current_user.business_id, invoice.branch_id, channel)
    zone = await get_business_tz(db, current_user.business_id)
    qr_url = None
    if invoice.synced and channel in ("print", "whatsapp"):
        qr_url = await link_url_for_sale(
            db, current_user.business_id, sale_id, current_user.id,
            str(request.base_url) if request is not None else None,
        )
    return build_receipt_model(invoice, layout, channel, zone=zone, qr_url=qr_url), invoice


@router.get(
    "/{sale_id}/receipt",
    status_code=status.HTTP_200_OK,
)
async def receipt(
    request: Request,
    sale_id: UUID,
    channel: str = Query(default="print", pattern="^(print|whatsapp|link)$"),
    format: str = Query(default="json", pattern="^(json|html|pdf|text)$"),
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    """The sale rendered with the layout assigned to ``channel`` (branch assignment, else default).

    ``json`` returns the render model every renderer consumes; ``html``, ``pdf`` and ``text``
    (WhatsApp) are renderings of that same model.
    """
    model, invoice = await _receipt_model(db, current_user, sale_id, channel, request)
    if format == "json":
        return model
    if format == "html":
        return HTMLResponse(render_html(model))
    if format == "text":
        return PlainTextResponse(render_text(model))
    return Response(
        render_pdf(model),
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="receipt-{invoice.sale_number}.pdf"'},
    )


@router.get(
    "/{sale_id}/thermal",
    response_model=ThermalReceiptData,
    status_code=status.HTTP_200_OK,
)
async def thermal_receipt(
    request: Request,
    sale_id: UUID,
    current_user: User = Depends(require_permission("sales.view")),
    db=Depends(get_db),
):
    data = await get_thermal_receipt_data(db, sale_id, current_user.business_id)
    model, _invoice = await _receipt_model(db, current_user, sale_id, "print", request)
    data.receipt = model
    return data

"""Receipt layouts (settings.manage): CRUD, duplicate, channel assignments, preview."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status

from app.branch_scope import assert_branch_access
from app.database import get_db
from app.dependencies import require_permission
from app.models.business import Business
from app.models.user import User
from app.schemas.receipt_layout import (
    AssignmentResponse,
    AssignmentsPut,
    LayoutCreate,
    LayoutDuplicate,
    LayoutResponse,
    LayoutUpdate,
    PreviewRequest,
)
from app.services import receipt_layout_service as svc
from app.services.business_time import get_business_tz
from app.services.invoice_service import get_invoice_data
from app.services.receipt_layout_config import DESIGN_PAPER, default_config, designs_description
from app.services.receipt_link_service import link_url_for_sale
from app.services.receipt_render import LayoutView, build_receipt_model, render_html, render_text

router = APIRouter(prefix="/receipt-layouts", tags=["Receipt layouts"])


@router.get("", response_model=list[LayoutResponse])
async def list_layouts(
    current_user: User = Depends(require_permission("settings.manage")), db=Depends(get_db)
):
    return [svc.layout_dict(x) for x in await svc.list_layouts(db, current_user.business_id)]


@router.get("/designs")
async def designs(current_user: User = Depends(require_permission("settings.manage"))):
    """The built-in designs with their papers and default configs (also the pharmacy variant)."""
    return {"designs": designs_description(), "channels": ["print", "whatsapp", "link"]}


@router.get("/assignments", response_model=list[AssignmentResponse])
async def get_assignments(
    current_user: User = Depends(require_permission("settings.manage")), db=Depends(get_db)
):
    return [svc.assignment_dict(x) for x in await svc.list_assignments(db, current_user.business_id)]


@router.put("/assignments", response_model=list[AssignmentResponse])
async def put_assignments(
    data: AssignmentsPut,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    rows = await svc.put_assignments(
        db, current_user.business_id, current_user.id, [a.model_dump() for a in data.assignments]
    )
    return [svc.assignment_dict(x) for x in rows]


@router.post("/preview")
async def preview(
    data: PreviewRequest,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    """Render a layout (saved, or an unsaved config) against a real sale or the built-in sample."""
    if data.layout_id is not None:
        layout = await svc.get_layout(db, current_user.business_id, data.layout_id)
        design, paper = layout.design, data.paper or layout.paper
        raw = data.config if data.config is not None else layout.config
        name, layout_id = layout.name, str(layout.id)
    elif data.design is not None:
        design = data.design
        paper = data.paper or DESIGN_PAPER.get(design, "roll80")
        raw = data.config if data.config is not None else default_config(design)
        name, layout_id = "Preview", None
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "layout_id or design is required")
    svc.check_design_paper(design, paper)
    view = LayoutView.build(layout_id, name, design, paper, raw)

    zone = await get_business_tz(db, current_user.business_id)
    if data.sale_id is not None:
        sale = await get_invoice_data(db, data.sale_id, current_user.business_id)
        assert_branch_access(current_user, sale.branch_id)
        qr_url = None
        if sale.synced and data.channel == "print":
            qr_url = await link_url_for_sale(
                db, current_user.business_id, data.sale_id, current_user.id, None
            )
        mock = False
    else:
        business = await db.get(Business, current_user.business_id)
        sale = svc.mock_sale(business)
        qr_url = svc.SAMPLE_LINK
        mock = True
    model = build_receipt_model(sale, view, data.channel, zone=zone, qr_url=qr_url)
    return {
        "mock": mock,
        "model": model,
        "html": render_html(model),
        "text": render_text(model),
    }


@router.post("", response_model=LayoutResponse, status_code=status.HTTP_201_CREATED)
async def create_layout(
    data: LayoutCreate,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    layout = await svc.create_layout(
        db, current_user.business_id, current_user.id,
        name=data.name, design=data.design, paper=data.paper, config=data.config,
    )
    return svc.layout_dict(layout)


@router.get("/{layout_id}", response_model=LayoutResponse)
async def get_layout(
    layout_id: UUID,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    return svc.layout_dict(await svc.get_layout(db, current_user.business_id, layout_id))


@router.put("/{layout_id}", response_model=LayoutResponse)
async def update_layout(
    layout_id: UUID,
    data: LayoutUpdate,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    layout = await svc.update_layout(
        db, current_user.business_id, current_user.id, layout_id, data.model_dump(exclude_unset=True)
    )
    return svc.layout_dict(layout)


@router.delete("/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_layout(
    layout_id: UUID,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    await svc.delete_layout(db, current_user.business_id, current_user.id, layout_id)


@router.post("/{layout_id}/duplicate", response_model=LayoutResponse, status_code=status.HTTP_201_CREATED)
async def duplicate_layout(
    layout_id: UUID,
    data: LayoutDuplicate | None = None,
    current_user: User = Depends(require_permission("settings.manage")),
    db=Depends(get_db),
):
    layout = await svc.duplicate_layout(
        db, current_user.business_id, current_user.id, layout_id, data.name if data else None
    )
    return svc.layout_dict(layout)

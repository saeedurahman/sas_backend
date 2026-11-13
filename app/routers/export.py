"""``GET /export/{dataset}`` – CSV / XLSX downloads (see ``app.services.export_datasets``)."""

from __future__ import annotations

from datetime import date, timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.branch_scope import scoped_branch_query
from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.database import get_db
from app.dependencies import (
    apply_business_timezone,
    require_permission,
    user_permission_keys_for_response,
)
from app.models.user import User
from app.services import export_datasets as ds
from app.services.business_time import get_business_tz

router = APIRouter(
    prefix="/export",
    tags=["Export"],
    dependencies=[Depends(apply_business_timezone)],
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_FORMATS = {"csv": "csv", "xlsx": "xlsx", "excel": "xlsx"}
_DEFAULT_DAYS = 30


def _allowed(user: User) -> list[ds.Dataset]:
    keys = set(user_permission_keys_for_response(user))
    return [
        d for d in ds.DATASETS.values() if d.extra_permission is None or d.extra_permission in keys
    ]


@router.get("")
async def list_datasets(
    current_user: User = Depends(require_permission("reports.export")),
):
    """Datasets this user may export, with their column names."""
    return {
        "max_rows": settings.export_max_rows,
        "datasets": [
            {
                "name": d.name,
                "description": d.description,
                "columns": [c.key for c in d.columns],
                "date_filter": d.uses_dates,
                "branch_filter": d.uses_branch,
                "formats": ["csv", "xlsx"],
            }
            for d in _allowed(current_user)
        ],
    }


@router.get("/{dataset}")
async def export_dataset(
    dataset: str,
    format: str = Query(default="csv", description="csv | xlsx"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    branch_id: UUID | None = Depends(scoped_branch_query),
    current_user: User = Depends(require_permission("reports.export")),
    db: AsyncSession = Depends(get_db),
):
    spec = ds.resolve_dataset(dataset.lower())
    if spec.extra_permission and spec.extra_permission not in set(
        user_permission_keys_for_response(current_user)
    ):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"Permission required: {spec.extra_permission}"
        )
    fmt = _FORMATS.get(format.lower())
    if fmt is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "format must be 'csv' or 'xlsx'")

    enforce_rate_limit("export", str(current_user.id), settings.rate_limit_export_user)

    zone = await get_business_tz(db, current_user.business_id)
    if date_to is None:
        from datetime import datetime

        date_to = datetime.now(zone).date()
    if date_from is None:
        date_from = date_to - timedelta(days=_DEFAULT_DAYS - 1)
    if date_from > date_to:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "date_from must not be after date_to"
        )

    ctx = ds.ExportCtx(
        business_id=current_user.business_id,
        zone=zone,
        date_from=date_from,
        date_to=date_to,
        branch_id=branch_id if spec.uses_branch else None,
    )
    prepared = await ds.prepare(db, spec, ctx)
    # End the request transaction now: the stream opens its own session.
    await db.commit()

    stamp = f"{date_from}_{date_to}" if spec.uses_dates else str(date_to)
    filename = f"{spec.name}_{stamp}.{fmt}"
    body = ds.stream_csv(prepared) if fmt == "csv" else ds.stream_xlsx(prepared)
    return StreamingResponse(
        body,
        media_type="text/csv; charset=utf-8" if fmt == "csv" else _XLSX,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
            "X-Row-Count": str(prepared.row_count),
        },
    )

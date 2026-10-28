from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.database import get_db
from app.dependencies import require_feature_flag, require_permission
from app.models.business import BusinessConfig
from app.models.user import User
from app.schemas.accounting import (
    ChartOfAccountResponse,
    ChartOfAccountTreeNode,
    CreateChartOfAccountRequest,
    CreateJournalEntryRequest,
    JournalEntryListResponse,
    JournalEntryResponse,
    UpdateChartOfAccountRequest,
    UpdateJournalEntryRequest,
)
from app.schemas.auth import MessageResponse
from app.services.accounting_coa_service import (
    create_chart_of_account,
    delete_chart_of_account,
    get_chart_of_account_by_id,
    list_chart_of_accounts,
    update_chart_of_account,
)
from app.services.accounting_journal_service import (
    create_journal_entry,
    delete_journal_entry,
    get_journal_entry_by_id,
    list_journal_entries,
    post_journal_entry,
    update_journal_entry,
)

router = APIRouter(prefix="/accounting", tags=["Accounting"])


@router.get(
    "/coa",
    status_code=status.HTTP_200_OK,
)
async def list_coa(
    active_only: bool = Query(default=False),
    tree: bool = Query(default=False),
    current_user: User = Depends(require_permission("accounting.coa.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
) -> list[ChartOfAccountResponse] | list[ChartOfAccountTreeNode]:
    return await list_chart_of_accounts(
        db,
        current_user.business_id,
        active_only=active_only,
        tree=tree,
        created_by=current_user.id,
    )


@router.get(
    "/coa/{account_id}",
    response_model=ChartOfAccountResponse,
    status_code=status.HTTP_200_OK,
)
async def get_coa(
    account_id: UUID,
    current_user: User = Depends(require_permission("accounting.coa.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await get_chart_of_account_by_id(
        db,
        account_id,
        current_user.business_id,
    )


@router.post(
    "/coa",
    response_model=ChartOfAccountResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_coa(
    data: CreateChartOfAccountRequest,
    current_user: User = Depends(require_permission("accounting.coa.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await create_chart_of_account(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.put(
    "/coa/{account_id}",
    response_model=ChartOfAccountResponse,
    status_code=status.HTTP_200_OK,
)
async def update_coa(
    account_id: UUID,
    data: UpdateChartOfAccountRequest,
    current_user: User = Depends(require_permission("accounting.coa.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await update_chart_of_account(
        db,
        account_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/coa/{account_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_coa(
    account_id: UUID,
    current_user: User = Depends(require_permission("accounting.coa.manage")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    await delete_chart_of_account(
        db,
        account_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Chart of accounts entry deleted")


@router.get(
    "/journal-entries",
    response_model=list[JournalEntryListResponse],
    status_code=status.HTTP_200_OK,
)
async def list_journal_entries_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    date_from: date | None = Query(default=None),
    date_to: date | None = Query(default=None),
    branch_id: UUID | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
    current_user: User = Depends(require_permission("accounting.journal.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await list_journal_entries(
        db,
        current_user.business_id,
        status_filter=status_filter,
        date_from=date_from,
        date_to=date_to,
        branch_id=branch_id,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryResponse,
    status_code=status.HTTP_200_OK,
)
async def get_journal_entry(
    entry_id: UUID,
    current_user: User = Depends(require_permission("accounting.journal.view")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await get_journal_entry_by_id(
        db,
        entry_id,
        current_user.business_id,
    )


@router.post(
    "/journal-entries",
    response_model=JournalEntryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_journal_entry_endpoint(
    data: CreateJournalEntryRequest,
    current_user: User = Depends(require_permission("accounting.journal.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await create_journal_entry(
        db,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.put(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryResponse,
    status_code=status.HTTP_200_OK,
)
async def update_journal_entry_endpoint(
    entry_id: UUID,
    data: UpdateJournalEntryRequest,
    current_user: User = Depends(require_permission("accounting.journal.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await update_journal_entry(
        db,
        entry_id,
        current_user.business_id,
        data,
        current_user.id,
    )


@router.delete(
    "/journal-entries/{entry_id}",
    response_model=MessageResponse,
    status_code=status.HTTP_200_OK,
)
async def delete_journal_entry_endpoint(
    entry_id: UUID,
    current_user: User = Depends(require_permission("accounting.journal.create")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    await delete_journal_entry(
        db,
        entry_id,
        current_user.business_id,
        current_user.id,
    )
    return MessageResponse(message="Journal entry deleted")


@router.post(
    "/journal-entries/{entry_id}/post",
    response_model=JournalEntryResponse,
    status_code=status.HTTP_200_OK,
)
async def post_journal_entry_endpoint(
    entry_id: UUID,
    current_user: User = Depends(require_permission("accounting.journal.post")),
    _config: BusinessConfig = Depends(require_feature_flag("enable_accounting")),
    db=Depends(get_db),
):
    return await post_journal_entry(
        db,
        entry_id,
        current_user.business_id,
        current_user.id,
    )

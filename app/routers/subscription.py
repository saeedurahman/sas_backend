from datetime import datetime

from fastapi import APIRouter, Depends, status
from sqlalchemy import select

from app.database import get_db
from app.dependencies import get_current_user, subscription_exempt
from app.models.platform import Plan
from app.models.user import User
from app.schemas.base import BaseSchema
from app.services.subscription import (
    PAYMENT_INSTRUCTIONS_KEY,
    compute_state,
    contact_from_settings,
    load_platform_settings,
)

# Every route here works for expired/suspended/grace businesses (the app must be able
# to show the renewal screen and contact details).
router = APIRouter(
    prefix="/subscription",
    tags=["Subscription"],
    dependencies=[Depends(subscription_exempt)],
)


class ContactInfo(BaseSchema):
    phone: str = ""
    whatsapp: str = ""
    email: str = ""


class SubscriptionStatusResponse(BaseSchema):
    plan_name: str | None = None
    status: str  # trial | active | grace | expired | suspended | cancelled
    trial_ends_at: datetime | None = None
    subscription_ends_at: datetime | None = None
    days_left: int | None = None
    in_grace: bool
    contact: ContactInfo
    payment_instructions: str = ""


@router.get(
    "/status",
    response_model=SubscriptionStatusResponse,
    status_code=status.HTTP_200_OK,
)
async def subscription_status(
    current_user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    business = current_user.business
    state = compute_state(business)
    plan_name = None
    if business.plan_id is not None:
        plan_name = (
            await db.execute(select(Plan.name).where(Plan.id == business.plan_id))
        ).scalar_one_or_none()
    values = await load_platform_settings(db)
    return SubscriptionStatusResponse(
        plan_name=plan_name,
        status=state.status,
        trial_ends_at=state.trial_ends_at,
        subscription_ends_at=state.subscription_ends_at,
        days_left=state.days_left,
        in_grace=state.in_grace,
        contact=ContactInfo(**contact_from_settings(values)),
        payment_instructions=values.get(PAYMENT_INSTRUCTIONS_KEY, "") or "",
    )

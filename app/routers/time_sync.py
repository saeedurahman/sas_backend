"""GET /time - tiny unauthenticated reachability + clock-skew probe (``/health`` stays)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from app.core.rate_limit import ip_rate_limit
from app.schemas.sync import TimeResponse

router = APIRouter(tags=["Sync"])


@router.get(
    "/time",
    response_model=TimeResponse,
    dependencies=[Depends(ip_rate_limit("time-ip", "rate_limit_time_ip"))],
)
async def server_time():
    now = datetime.now(timezone.utc)
    return TimeResponse(server_time=now, epoch_ms=int(now.timestamp() * 1000))

"""Public license endpoints for the offline edition (no tenant or admin auth)."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app.core.rate_limit import ip_rate_limit
from app.database import get_db
from app.services.license_service import signed_revocation_list

router = APIRouter(prefix="/licenses", tags=["Licenses (public)"])


@router.get(
    "/revocations",
    dependencies=[Depends(ip_rate_limit("license-revocations", "rate_limit_license_revocations"))],
)
async def revocation_list(db=Depends(get_db)):
    """Signed list of revoked license ids.

    ``signature`` is Ed25519 (base64url) over the canonical JSON (sorted keys, no
    whitespace) of this object without the ``signature`` field. Verify with
    ``app.licensing.keys.verify_revocation_list`` and LICENSE_PUBLIC_KEY.
    """
    return JSONResponse(await signed_revocation_list(db), headers={"Cache-Control": "no-store"})

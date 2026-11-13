from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from pydantic import BaseModel

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.dependencies import require_any_permission
from app.models.user import User
from app.services.upload_service import store_image

router = APIRouter(prefix="/uploads", tags=["Uploads"])


class ImageUploadResponse(BaseModel):
    url: str


@router.post("/image", response_model=ImageUploadResponse, status_code=status.HTTP_201_CREATED)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    current_user: User = Depends(require_any_permission("products.update", "settings.manage")),
):
    """Multipart upload (field ``file``): JPEG/PNG/WebP/GIF up to UPLOAD_MAX_IMAGE_BYTES.

    Stored on Cloudinary when CLOUDINARY_URL is set, otherwise under ``/static/uploads``
    (development only). Returns the public image URL to put in ``image_url``/logo fields.
    """
    enforce_rate_limit("upload-user", str(current_user.id), settings.rate_limit_upload_user)
    url = await store_image(file, current_user.business_id, str(request.base_url))
    return ImageUploadResponse(url=url)

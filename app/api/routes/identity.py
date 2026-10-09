from typing import Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.core.rate_limit import verification_limiter
from app.core.security import require_api_key
from app.schemas.identity import VerifyIdentityResponse, VerifyPhotoResponse
from app.services.identity_verification import verify_identity
from app.services.photo_verification import verify_photo

router = APIRouter(tags=["Identity"])


@router.post(
    "/verify-identity",
    response_model=VerifyIdentityResponse,
    # El orden importa: primero se identifica el proyecto, luego se cuenta.
    dependencies=[Depends(require_api_key), Depends(verification_limiter)],
)
async def verify_identity_endpoint(
    id_image: UploadFile = File(...),
    video: UploadFile = File(...),
    expected_phrase: Optional[str] = Form(None),
    include_images: bool = Form(False),
):
    """Rostro del video vs identificacion + frase hablada en el mismo video.

    `result.approved` solo es true si ninguna validacion de `checks` fallo.
    `include_images=true` agrega los recortes de cara en base64 (pesan ~70 KB).
    """
    return await verify_identity(id_image, video, expected_phrase, include_images)


@router.post(
    "/verify-photo",
    response_model=VerifyPhotoResponse,
    dependencies=[Depends(require_api_key), Depends(verification_limiter)],
)
async def verify_photo_endpoint(
    id_image: UploadFile = File(...),
    photo: UploadFile = File(...),
    include_images: bool = Form(False),
):
    """Rostro de una foto vs identificacion, con validaciones de calidad y anti-spoofing de la foto.

    `result.approved` solo es true si ninguna validacion de `checks` fallo.
    `include_images=true` agrega los recortes de cara en base64.
    """
    return await verify_photo(id_image, photo, include_images)

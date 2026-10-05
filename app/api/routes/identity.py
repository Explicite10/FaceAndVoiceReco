from typing import Optional

from fastapi import APIRouter, File, Form, UploadFile

from app.schemas.identity import VerifyIdentityResponse
from app.services.identity_verification import verify_identity

router = APIRouter(tags=["Identity"])


@router.post("/verify-identity", response_model=VerifyIdentityResponse)
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

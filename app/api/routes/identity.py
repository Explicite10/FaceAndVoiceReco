from typing import Optional

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from app.core.config import settings
from app.core.rate_limit import challenge_limiter, verification_limiter
from app.core.security import require_api_key
from app.schemas.identity import ChallengeResponse, VerifyIdentityResponse, VerifyPhotoResponse
from app.services.challenge import GESTURE_LABELS, create_challenge
from app.services.identity_verification import verify_identity
from app.services.photo_verification import verify_photo

router = APIRouter(tags=["Identity"])


@router.post(
    "/challenge",
    response_model=ChallengeResponse,
    dependencies=[Depends(require_api_key), Depends(challenge_limiter)],
)
def create_challenge_endpoint(request: Request):
    """Reto de un solo uso para /api/verify-identity: gestos y frase aleatorios.

    En el video, el usuario hace los gestos en el orden indicado y despues lee la
    frase. Vence en `expires_in_sec` y solo sirve para un video.
    """
    c = create_challenge(getattr(request.state, "client", None))
    return {
        "challenge_id": c["challenge_id"],
        "phrase": c["phrase"],
        "gestures": [{"gesture": g, "instruction": GESTURE_LABELS[g]} for g in c["gestures"]],
        "expires_in_sec": settings.challenge_ttl_sec,
    }


@router.post(
    "/verify-identity",
    response_model=VerifyIdentityResponse,
    # El orden importa: primero se identifica el proyecto, luego se cuenta.
    dependencies=[Depends(require_api_key), Depends(verification_limiter)],
)
async def verify_identity_endpoint(
    request: Request,
    id_image: UploadFile = File(...),
    video: UploadFile = File(...),
    challenge_id: Optional[str] = Form(None, description="De POST /api/challenge. Obligatorio salvo REQUIRE_CHALLENGE=false."),
    expected_phrase: Optional[str] = Form(
        None, description="Solo sin challenge_id (REQUIRE_CHALLENGE=false). Con reto se ignora."
    ),
    match_threshold_percent: Optional[float] = Form(
        None,
        ge=1,
        le=100,
        description="Porcentaje minimo de similitud facial para aprobar. Si se omite: 70.",
    ),
):
    """Rostro del video vs identificacion + gestos del reto + frase hablada, en el mismo video.

    `result.approved` solo es true si ninguna validacion de `checks` fallo.
    `match_threshold_percent` fija el % minimo de similitud en cada tramo (default 70).
    Un `challenge_id` vencido, usado o inexistente responde 400 sin analizar el video.
    """
    return await verify_identity(
        id_image,
        video,
        expected_phrase,
        match_threshold_percent,
        challenge_id=challenge_id,
        client=getattr(request.state, "client", None),
    )


@router.post(
    "/verify-photo",
    response_model=VerifyPhotoResponse,
    dependencies=[Depends(require_api_key), Depends(verification_limiter)],
)
async def verify_photo_endpoint(
    id_image: UploadFile = File(...),
    photo: UploadFile = File(...),
    match_threshold_percent: Optional[float] = Form(
        None,
        ge=1,
        le=100,
        description="Porcentaje minimo de similitud facial para aprobar. Si se omite: 70.",
    ),
):
    """Rostro de una foto vs identificacion, con validaciones de calidad y anti-spoofing de la foto.

    `result.approved` solo es true si ninguna validacion de `checks` fallo.
    `match_threshold_percent` fija el % minimo de similitud (default 70).
    """
    return await verify_photo(id_image, photo, match_threshold_percent)

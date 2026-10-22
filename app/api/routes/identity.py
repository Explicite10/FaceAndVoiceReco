import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from app import db
from app.core.acceso import require_acceso
from app.core.config import settings
from app.core.rate_limit import challenge_limiter, qr_scan_limiter, verification_limiter
from app.schemas.identity import ChallengeResponse, VerifyIdentityResponse, VerifyPhotoResponse
from app.services import expediente
from app.services.challenge import GESTURE_LABELS, create_challenge
from app.services.identity_verification import verify_identity
from app.services.photo_verification import verify_photo

router = APIRouter(tags=["Identity"])
logger = logging.getLogger("uvicorn.error")

# Analisis simultaneos: cada uno ocupa varios nucleos un buen rato. Los demas
# esperan su turno; si tardan demasiado, se les pide intentar despues.
_turnos = asyncio.Semaphore(settings.max_concurrent_verifications)
_ESPERA_MAX_SEC = 90


async def _con_turno(coro):
    try:
        await asyncio.wait_for(_turnos.acquire(), timeout=_ESPERA_MAX_SEC)
    except asyncio.TimeoutError:
        coro.close()
        raise HTTPException(status_code=503, detail="Hay muchas verificaciones en curso. Intenta en un momento.",
                            headers={"Retry-After": "30"})
    try:
        return await coro
    finally:
        _turnos.release()


def _diagnosticar_voz(reporte: dict, diag: str | None) -> None:
    """Si la voz fallo, deja en el log el error exacto del audio y los datos tecnicos del
    navegador (formato, estado del microfono). Solo log: no se guarda en la base."""
    voz = reporte.get("checks", {}).get("voice", {})
    fallos = [c["code"] for c in voz.values() if c.get("status") == "failed" and c.get("code")]
    if not fallos:
        return
    error = next((e.get("message") for e in reporte.get("errors", []) if e.get("component") == "voice"), None)
    logger.warning("Voz fallo %s | servidor: %s | navegador: %s", fallos, error, (diag or "sin diagnostico")[:1000])


async def _registrar(request: Request, tipo: str, reporte: dict, archivos: dict[str, UploadFile]) -> None:
    """Suma la prueba a la sesion del QR (resultado y codigos de lo que fallo) y guarda su
    expediente en disco: la INE y el video, o la INE y la foto."""
    sid = getattr(request.state, "sesion", None)
    if not sid:
        return
    fallos = [c["code"] for grupo in reporte.get("checks", {}).values() for c in grupo.values()
              if c.get("status") == "failed" and c.get("code")]
    # Lo mismo que vio la persona (checks y porcentajes) para "Ver registro" del panel;
    # sin errores tecnicos internos ni metadatos.
    detalle = {k: reporte.get(k) for k in ("result", "checks", "details", "processing_time_ms")}
    intento_id = await db.registrar_prueba(sid, tipo, bool(reporte["result"]["approved"]), fallos,
                                           reporte.get("processing_time_ms"), detalle)
    if intento_id is None:
        return
    # Si guardar el expediente falla, la persona igual recibe su resultado: queda en el log.
    try:
        guardados = await expediente.guardar(sid, intento_id, archivos)
        try:
            await db.registrar_evidencias(intento_id, guardados)
        except Exception:
            expediente.borrar_intento(sid, intento_id)  # sin rutas en la base, no se dejan archivos huerfanos
            raise
    except Exception:
        logger.exception("No se pudo guardar el expediente del intento %s", intento_id)


@router.post("/visita", status_code=204, include_in_schema=False,
             dependencies=[Depends(require_acceso), Depends(challenge_limiter)])
async def visita(request: Request) -> Response:
    """La pagina de la prueba cargo en un navegador real: ahora si se registra la sesion.

    El enlace del QR solo entrega la cookie; asi los robots que nada mas visitan el
    enlace (revisores de correo/chat, escaneres) no aparecen en el tablero.
    """
    sid = getattr(request.state, "sesion", None)
    if sid and not await db.sesion_existe(sid):
        qr_scan_limiter(request)  # registros nuevos por IP: aqui ya solo llegan navegadores reales
        await db.asegurar_sesion(sid)
    return Response(status_code=204)


@router.post(
    "/challenge",
    response_model=ChallengeResponse,
    dependencies=[Depends(require_acceso), Depends(challenge_limiter)],
)
async def create_challenge_endpoint(request: Request):
    """Reto de un solo uso para /api/verify-identity: gestos y frase aleatorios.

    En el video, el usuario hace los gestos en el orden indicado y despues lee la
    frase. Vence en `expires_in_sec` y solo sirve para un video.
    """
    c = create_challenge(getattr(request.state, "client", None))
    # Para el embudo del panel: la persona activo la camara para grabar.
    if getattr(request.state, "sesion", None):
        await db.registrar_reto(request.state.sesion)
    return {
        "challenge_id": c["challenge_id"],
        "phrase": c["phrase"],
        "gestures": [{"gesture": g, "instruction": GESTURE_LABELS[g]} for g in c["gestures"]],
        "expires_in_sec": settings.challenge_ttl_sec,
    }


@router.post(
    "/verify-identity",
    response_model=VerifyIdentityResponse,
    # El orden importa: primero se identifica la sesion (o proyecto), luego se cuenta.
    dependencies=[Depends(require_acceso), Depends(verification_limiter)],
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
    frase_inicio: Optional[float] = Form(
        None, ge=0, le=600, description="Segundo del video en que se mostro la frase (lo mide la pagina)."
    ),
    frase_fin: Optional[float] = Form(
        None, ge=0, le=600, description="Segundo del video en que termino la lectura de la frase."
    ),
    diag: Optional[str] = Form(
        None, max_length=1500, description="Datos tecnicos de la grabacion (navegador, formato, microfono)."
    ),
):
    """Rostro del video vs identificacion + gestos del reto + frase hablada, en el mismo video.

    Con `frase_inicio` (y opcionalmente `frase_fin`) la INE se compara solo en el tramo
    de la frase, la voz solo se escucha ahi y en los gestos se revisa que sea la misma
    persona. El tramo solo se usa si empieza despues del ultimo gesto encontrado.

    `result.approved` solo es true si ninguna validacion de `checks` fallo.
    `match_threshold_percent` fija el % minimo de similitud en cada tramo (default 70).
    Un `challenge_id` vencido, usado o inexistente responde 400 sin analizar el video.
    """
    reporte = await _con_turno(verify_identity(
        id_image,
        video,
        expected_phrase,
        match_threshold_percent,
        challenge_id=challenge_id,
        client=getattr(request.state, "client", None),
        ventana_frase=(frase_inicio, frase_fin) if frase_inicio is not None else None,
    ))
    _diagnosticar_voz(reporte, diag)
    await _registrar(request, "video", reporte, {"ine": id_image, "video": video})
    return reporte


@router.post(
    "/verify-photo",
    response_model=VerifyPhotoResponse,
    dependencies=[Depends(require_acceso), Depends(verification_limiter)],
)
async def verify_photo_endpoint(
    request: Request,
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
    reporte = await _con_turno(verify_photo(id_image, photo, match_threshold_percent))
    await _registrar(request, "foto", reporte, {"ine": id_image, "foto": photo})
    return reporte

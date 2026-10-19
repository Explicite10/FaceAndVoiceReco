import asyncio
import os
import time

import numpy as np
from fastapi import HTTPException, UploadFile

from app.core.config import settings
from app.services.audio_liveness import verify_voice_from_path
from app.services.challenge import consume_challenge
from app.services.errors import CheckError
from app.services.face_track import track_video
from app.services.gestures import analyze_gestures
from app.services.liveness import check_liveness
from app.services.report import build_report
from app.services.face_service import (
    compare_id_and_video_path,
    ensure_image,
    id_embedding,
    read_as_np_rgb,
)
from app.services.upload_io import ALLOWED_VIDEO_EXT, save_upload_to_temp

UPLOAD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "uploads")


async def _run_part(coro) -> dict:
    """Aisla cada analisis: un fallo en rostro no debe tirar el resultado de voz."""
    try:
        return {"ok": True, "status_code": 200, "error": None, "error_code": None, "result": await coro}
    except HTTPException as exc:
        return {
            "ok": False,
            "status_code": exc.status_code,
            "error": str(exc.detail),
            "error_code": getattr(exc, "code", None),
            "result": None,
        }


async def _check_gestures(track: dict, gestures: list[str]) -> dict:
    if not len(track["times"]):
        raise CheckError(422, "unreadable_video", "No se pudo leer el video para revisar los gestos.")
    return analyze_gestures(track, gestures)


async def _compare_face(id_emb: np.ndarray | CheckError, video_path: str, threshold_percent: float) -> dict:
    if isinstance(id_emb, CheckError):
        raise id_emb
    return await compare_id_and_video_path(id_emb, video_path, threshold_percent)


# Tolerancia entre el reloj de la pagina y los tiempos que mide el servidor.
_TOLERANCIA_VENTANA_SEC = 1.0


def ventana_valida(ventana: tuple[float, float | None] | None, gestures: dict | None) -> tuple[float, float | None] | None:
    """El tramo de la frase que manda la pagina, solo si cuadra con el video.

    Debe empezar despues del ultimo gesto que el servidor encontro en el video:
    asi no se puede apuntar el tramo a otra parte para esconder algo. Si no
    cuadra, se escucha el audio completo, como antes.
    """
    if not ventana or not gestures or not gestures.get("ok"):
        return None
    inicio, fin = ventana
    ultimo = gestures["result"].get("last_gesture_sec")
    if ultimo is None or inicio < ultimo - _TOLERANCIA_VENTANA_SEC:
        return None
    if fin is not None and fin <= inicio:
        fin = None
    return (inicio, fin)


async def embed_id_image(np_id: np.ndarray) -> np.ndarray:
    return await asyncio.to_thread(id_embedding, np_id)


async def analyze_video(
    video_path: str,
    id_emb: np.ndarray | CheckError,
    expected_phrase: str | None,
    match_threshold_percent: float,
    challenge: dict | None,
    started: float,
    ventana_frase: tuple[float, float | None] | None = None,
) -> dict:
    """Voz, rostro, prueba de vida y gestos sobre un video ya guardado en disco.

    `id_emb` es el vector de la INE, o el error de no haber encontrado su cara:
    asi ese caso sale en el reporte como cualquier otra validacion fallida.
    """
    # Secuencial a proposito: todo es CPU-bound y en paralelo se pelea por los
    # mismos cores sin terminar antes. Los gestos van antes que la voz: con ellos
    # se valida el tramo de la frase que manda la pagina.
    face = await _run_part(_compare_face(id_emb, video_path, match_threshold_percent))
    # Una sola pasada de MediaPipe para labios y gestos.
    track = await asyncio.to_thread(track_video, video_path, challenge is not None)
    gestures = await _run_part(_check_gestures(track, challenge["gestures"])) if challenge else None
    voice = await _run_part(verify_voice_from_path(video_path, expected_phrase, ventana_valida(ventana_frase, gestures)))
    # Los labios se miden en todo el video: el silencio de los gestos es su referencia.
    liveness = await _run_part(check_liveness(video_path, face, track))

    return build_report(
        face=face,
        voice=voice,
        liveness=liveness,
        challenge=challenge,
        gestures=gestures,
        elapsed_ms=int((time.time() - started) * 1000),
    )


async def verify_identity(
    id_image: UploadFile,
    video: UploadFile,
    expected_phrase: str | None = None,
    match_threshold_percent: float | None = None,
    challenge_id: str | None = None,
    client: str | None = None,
) -> dict:
    t0 = time.time()
    if match_threshold_percent is None:
        match_threshold_percent = settings.face_match_percent

    # El reto se consume antes de procesar nada: un id invalido no gasta CPU,
    # y un id usado una vez no sirve para reintentar con otro video.
    challenge = None
    if challenge_id:
        challenge = consume_challenge(challenge_id, client)
        expected_phrase = challenge["phrase"]
    elif settings.require_challenge:
        raise CheckError(400, "challenge_required", "Falta challenge_id: pide un reto en POST /api/challenge.")

    ensure_image(id_image, "id_image")
    np_id = await read_as_np_rgb(id_image, "id_image")
    try:
        id_emb = await embed_id_image(np_id)
    except CheckError as exc:
        id_emb = exc

    # Se guarda una sola vez: el stream del upload no se puede leer dos veces.
    temp_path = await save_upload_to_temp(
        video, UPLOAD_DIR, allowed_ext=ALLOWED_VIDEO_EXT, max_mb=settings.max_video_mb
    )
    try:
        return await analyze_video(temp_path, id_emb, expected_phrase, match_threshold_percent, challenge, t0)
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

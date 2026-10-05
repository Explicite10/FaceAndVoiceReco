import os
import time

from fastapi import HTTPException, UploadFile

from app.services.audio_liveness import verify_voice_from_path
from app.services.liveness import check_liveness
from app.services.report import build_report
from app.services.face_service import (
    compare_image_np_and_video_path,
    ensure_image,
    read_as_np_rgb,
)
from app.services.upload_io import ALLOWED_VIDEO_EXT, save_upload_to_temp


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


async def verify_identity(
    id_image: UploadFile,
    video: UploadFile,
    expected_phrase: str | None = None,
    include_images: bool = False,
) -> dict:
    t0 = time.time()

    ensure_image(id_image, "id_image")
    np_id = await read_as_np_rgb(id_image)

    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    upload_dir = os.path.join(os.path.dirname(app_dir), "uploads")
    # Se guarda una sola vez: el stream del upload no se puede leer dos veces.
    temp_path = await save_upload_to_temp(
        video, upload_dir, allowed_ext=ALLOWED_VIDEO_EXT, max_mb=80
    )

    try:
        # Secuencial a proposito: ambos son CPU-bound y en paralelo se pelean
        # por los mismos cores sin terminar antes.
        voice = await _run_part(verify_voice_from_path(temp_path, expected_phrase))
        face = await _run_part(compare_image_np_and_video_path(np_id, temp_path))
        liveness = await _run_part(check_liveness(temp_path, face))
    finally:
        try:
            os.remove(temp_path)
        except Exception:
            pass

    return build_report(
        face=face,
        voice=voice,
        liveness=liveness,
        elapsed_ms=int((time.time() - t0) * 1000),
        include_images=include_images,
    )

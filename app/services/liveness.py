import asyncio

from app.core.config import settings
from app.services.errors import CheckError
from app.services.lip_sync import analyze_lip_sync


async def check_liveness(video_path: str, face_part: dict, track: dict,
                         ventana: tuple[float, float | None] | None = None) -> dict:
    """Prueba de vida: la cara no debe ser una pantalla/foto y la boca debe ir con la voz.

    Los puntajes del detector de suplantacion ya vienen calculados por tramo en el
    resultado del rostro; aqui se agregan y se corre la sincronia labios-voz.
    """
    if not face_part["ok"]:
        raise CheckError(422, "liveness_no_face", "No hay rostro en el video para evaluar la prueba de vida.")

    segment_scores = [s["live_score"] for s in face_part["result"]["segments"]]
    found = [x for x in segment_scores if x is not None]
    real_score = sum(found) / len(found) if found else 0.0
    anti_spoof = {
        "passed": real_score >= settings.antispoof_threshold,
        "real_score": round(real_score, 4),
        "threshold": settings.antispoof_threshold,
        "segment_scores": segment_scores,
    }

    lip_sync = await asyncio.to_thread(analyze_lip_sync, video_path, track, ventana)

    is_live = anti_spoof["passed"] and lip_sync["passed"]
    return {
        "decision": "live" if is_live else "spoof",
        "is_live": is_live,
        "anti_spoof": anti_spoof,
        "lip_sync": lip_sync,
    }

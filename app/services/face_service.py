# app/services/face_service.py
from io import BytesIO
import numpy as np
from PIL import Image, ImageOps
from fastapi import HTTPException, UploadFile
import math
import asyncio

from app.core.config import settings
from app.services.errors import CheckError
from app.services.upload_io import read_upload_limited
from app.services.face_engine import (
    cosine_similarity, detect_faces, embed, pick_best_face
)
from app.services.video_face import best_faces_by_segment

# MPO es como Pillow reporta los JPEG de varias capas de algunos celulares.
ALLOWED_IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}


def ensure_image(upload: UploadFile, field_name: str) -> None:
    if not upload:
        raise HTTPException(status_code=400, detail=f"Falta el archivo: {field_name}")
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"'{field_name}' debe ser una imagen.")


async def read_as_np_rgb(upload: UploadFile, field_name: str) -> np.ndarray:
    raw = await read_upload_limited(upload, settings.max_image_mb, f"'{field_name}'")
    if not raw:
        raise HTTPException(status_code=400, detail=f"'{field_name}' está vacía.")
    try:
        img = Image.open(BytesIO(raw))  # solo lee el encabezado
    except Exception:
        raise HTTPException(status_code=400, detail=f"No pude leer '{field_name}'. ¿Formato válido?")

    # Se valida el formato real, no la extension ni el content-type que manda el cliente.
    if img.format not in ALLOWED_IMAGE_FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"'{field_name}' tiene formato {img.format}; se aceptan JPEG, PNG y WEBP.",
        )
    # Antes de decodificar: una imagen chica en bytes puede ocupar GBs en memoria.
    megapixels = img.width * img.height / 1_000_000
    if megapixels > settings.max_image_megapixels:
        raise HTTPException(
            status_code=413,
            detail=f"'{field_name}' es demasiado grande ({megapixels:.0f} MP; máximo {settings.max_image_megapixels} MP).",
        )

    try:
        # Las fotos de celular suelen venir giradas con una etiqueta EXIF; sin
        # aplicarla, la cara queda de lado y el detector no la encuentra.
        pil = ImageOps.exif_transpose(img).convert("RGB")
        return np.array(pil)  # RGB
    except Exception:
        raise HTTPException(status_code=400, detail=f"No pude leer '{field_name}'. ¿Archivo dañado?")


def similarity_to_percent_sigmoid(sim: float, mid: float, k: float) -> float:
    """sim suele estar entre ~0.0 y ~1.0 (cosine similarity).

    NO es probabilidad. Esto es un mapeo UI-friendly con curva sigmoid.
    """

    sim = max(0.0, min(1.0, float(sim)))
    x = (sim - mid) * k
    y = 1.0 / (1.0 + math.exp(-x))
    return round(y * 100.0, 2)


def percent_to_similarity(percent: float, mid: float, k: float) -> float:
    """Inversa de similarity_to_percent_sigmoid: la similitud coseno que da ese %."""
    p = min(max(percent / 100.0, 1e-6), 1.0 - 1e-6)
    # Cerca de 100% la inversa pasa de 1, y la similitud coseno no puede.
    return round(min(1.0, mid + math.log(p / (1.0 - p)) / k), 4)


def id_embedding(np_id: np.ndarray) -> np.ndarray:
    """Vector de la cara de la identificacion (128 numeros). Falla si no hay cara."""
    id_face = pick_best_face(detect_faces(np_id))
    if id_face is None:
        raise CheckError(422, "id_face_not_found", "No se detectó rostro en la identificación.")
    return embed(np_id, id_face)


def compare_id_against_segments(
    id_emb: np.ndarray, segments: list[dict | None], face_stats: dict, threshold_percent: float
) -> dict:
    """Compara la cara de la identificacion contra la mejor cara de cada tramo.

    Sin tramo de frase: el porcentaje es el promedio de los tramos, pero solo es
    match si TODOS llegan a threshold_percent: con el promedio solo, un tramo con
    otra persona quedaria escondido (0.90 + 0.90 + 0.10 promedia 0.63 y pasaria).

    Con tramo de frase (role "frase"): la INE se compara solo contra la frase,
    donde la persona esta quieta y de frente. En los tramos de gestos se revisa
    que sea la misma persona que leyo la frase (video contra video): asi un tramo
    de perfil no tumba la verificacion, pero cambiar de persona si se detecta.
    """

    mid = settings.sim_threshold
    frase = next((s for s in segments if s and s.get("role") == "frase"), None)
    if frase is not None:
        return _compare_por_frase(id_emb, segments, frase, face_stats, threshold_percent)
    results = []
    for index, seg in enumerate(segments, start=1):
        if seg is None:
            results.append({
                "index": index,
                "time_sec": None,
                "face_found": False,
                "raw_similarity": None,
                "similarity_percent": 0.0,
                "is_match": False,
                "live_score": None,
            })
            continue

        sim = cosine_similarity(id_emb, embed(seg["crop"], seg["face"]))
        # Se compara el % que se muestra, para que lo visto y lo decidido coincidan.
        percent = similarity_to_percent_sigmoid(sim, mid, settings.score_k)
        results.append({
            "index": index,
            "time_sec": seg["time_sec"],
            "face_found": True,
            "raw_similarity": round(sim, 6),
            "similarity_percent": percent,
            "is_match": percent >= threshold_percent,
            "live_score": seg["live_score"],
            # Si la cara elegida de este tramo miraba de frente (en los gestos puede que no).
            "frontal": seg.get("frontal"),
        })

    found = [r["raw_similarity"] for r in results if r["face_found"]]
    # Unos pocos cuadros con caras extra se toleran: un falso positivo aislado
    # del detector no debe rechazar a alguien que esta solo frente a la camara.
    multiple_faces = {
        "detected": face_stats["multi_face_frames"] >= settings.multi_face_min_frames,
        "max_faces_in_frame": face_stats["max_faces_in_frame"],
        "frames": face_stats["multi_face_frames"],
        "first_time_sec": face_stats["first_multi_face_sec"],
    }
    is_match = all(r["is_match"] for r in results) and not multiple_faces["detected"]
    if multiple_faces["detected"]:
        decision = "multiple_faces"
    else:
        decision = "match" if is_match else "no_match"

    return {
        "decision": decision,
        "is_match": is_match,
        "similarity_percent": round(float(np.mean([r["similarity_percent"] for r in results])), 2),
        "min_similarity": min(found) if found else None,
        "tolerance": percent_to_similarity(threshold_percent, mid, settings.score_k),
        "threshold_percent": float(threshold_percent),
        "segments": results,
        "multiple_faces": multiple_faces,
    }


def _multiple_faces(face_stats: dict) -> dict:
    return {
        "detected": face_stats["multi_face_frames"] >= settings.multi_face_min_frames,
        "max_faces_in_frame": face_stats["max_faces_in_frame"],
        "frames": face_stats["multi_face_frames"],
        "first_time_sec": face_stats["first_multi_face_sec"],
    }


def _compare_por_frase(
    id_emb: np.ndarray, segments: list[dict | None], frase: dict, face_stats: dict, threshold_percent: float
) -> dict:
    mid, k = settings.sim_threshold, settings.score_k
    frase_emb = embed(frase["crop"], frase["face"])
    sim_ine = cosine_similarity(id_emb, frase_emb)
    pct_ine = similarity_to_percent_sigmoid(sim_ine, mid, k)

    results = []
    for index, seg in enumerate(segments, start=1):
        if seg is None:
            results.append({"index": index, "role": None, "time_sec": None, "face_found": False,
                             "raw_similarity": None, "similarity_percent": 0.0, "is_match": False,
                             "same_person_percent": None, "live_score": None, "frontal": None})
            continue
        if seg is frase:
            results.append({"index": index, "role": "frase", "time_sec": seg["time_sec"], "face_found": True,
                            "raw_similarity": round(sim_ine, 6), "similarity_percent": pct_ine,
                            "is_match": pct_ine >= threshold_percent, "same_person_percent": None,
                            "live_score": seg["live_score"], "frontal": seg.get("frontal")})
            continue
        emb = embed(seg["crop"], seg["face"])
        sim_misma = cosine_similarity(frase_emb, emb)
        pct_misma = similarity_to_percent_sigmoid(sim_misma, mid, k)
        # Contra la INE solo como referencia: en los gestos no decide.
        sim_seg_ine = cosine_similarity(id_emb, emb)
        results.append({"index": index, "role": seg.get("role"), "time_sec": seg["time_sec"], "face_found": True,
                        "raw_similarity": round(sim_seg_ine, 6),
                        "similarity_percent": similarity_to_percent_sigmoid(sim_seg_ine, mid, k),
                        "is_match": pct_misma >= settings.same_person_percent,
                        "same_person_percent": pct_misma, "live_score": seg["live_score"],
                        "frontal": seg.get("frontal")})

    gestos = [r for r in results if r["role"] != "frase"]
    misma_persona = all(r["face_found"] and r["is_match"] for r in gestos)
    coincide_ine = pct_ine >= threshold_percent
    multiple_faces = _multiple_faces(face_stats)
    is_match = coincide_ine and misma_persona and not multiple_faces["detected"]
    if multiple_faces["detected"]:
        decision = "multiple_faces"
    elif not coincide_ine:
        decision = "no_match"
    elif not misma_persona:
        decision = "person_changed"
    else:
        decision = "match"
    return {
        "decision": decision,
        "is_match": is_match,
        "mode": "frase",
        # El porcentaje que se muestra es el de la INE contra la frase.
        "similarity_percent": pct_ine,
        "min_similarity": round(sim_ine, 6),
        "tolerance": percent_to_similarity(threshold_percent, mid, k),
        "threshold_percent": float(threshold_percent),
        "id_match": coincide_ine,
        "same_person": {
            "passed": misma_persona,
            "threshold_percent": settings.same_person_percent,
            "min_percent": min((r["same_person_percent"] for r in gestos if r["same_person_percent"] is not None), default=None),
        },
        "segments": results,
        "multiple_faces": multiple_faces,
    }


async def compare_id_and_video_path(
    id_emb: np.ndarray, video_path: str, threshold_percent: float, ventana: tuple[float, float | None] | None = None,
) -> dict:
    """Compara la identificacion (ya convertida con id_embedding) contra cada tramo del video en disco."""

    try:
        segments, face_stats = await asyncio.wait_for(
            asyncio.to_thread(best_faces_by_segment, video_path, segments=settings.face_segments, ventana=ventana),
            timeout=60,
        )
    except asyncio.TimeoutError:
        raise CheckError(
            408,
            "video_timeout",
            (
                "El procesamiento del video tardó demasiado (timeout). "
                "Prueba con un video más corto (3-10s)."
            ),
        )

    if all(seg is None for seg in segments):
        raise CheckError(
            422,
            "video_face_not_found",
            "No se detectó ninguna cara en el video. Prueba con mejor iluminación o rostro más cerca.",
        )

    return await asyncio.to_thread(compare_id_against_segments, id_emb, segments, face_stats, threshold_percent)

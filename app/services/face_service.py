# app/services/face_service.py
from io import BytesIO
import base64
import numpy as np
from PIL import Image, ImageOps
from fastapi import HTTPException, UploadFile
import math
import asyncio

from app.core.config import settings
from app.services.errors import CheckError
from app.services.face_engine import (
    cosine_similarity, detect_faces, embed, pick_best_face, to_bbox_dict
)
from app.services.video_face import best_faces_by_segment


def ensure_image(upload: UploadFile, field_name: str) -> None:
    if not upload:
        raise HTTPException(status_code=400, detail=f"Falta el archivo: {field_name}")
    if not upload.content_type or not upload.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail=f"'{field_name}' debe ser una imagen.")


async def read_as_np_rgb(upload: UploadFile) -> np.ndarray:
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="La imagen está vacía.")
    try:
        # Las fotos de celular suelen venir giradas con una etiqueta EXIF; sin
        # aplicarla, la cara queda de lado y el detector no la encuentra.
        pil = ImageOps.exif_transpose(Image.open(BytesIO(raw))).convert("RGB")
        return np.array(pil)  # RGB
    except Exception:
        raise HTTPException(status_code=400, detail="No pude leer la imagen. ¿Formato válido?")


def crop_face(np_rgb: np.ndarray, bbox: dict):
    pil_img = Image.fromarray(np_rgb)  # RGB
    width, height = pil_img.size
    top, right, bottom, left = bbox["top"], bbox["right"], bbox["bottom"], bbox["left"]

    top = max(0, top)
    left = max(0, left)
    bottom = min(height, bottom)
    right = min(width, right)

    return pil_img.crop((left, top, right, bottom))


def img_to_base64_jpeg(pil_img: Image.Image, quality: int = 95):
    buf = BytesIO()
    pil_img.save(buf, format="JPEG", quality=quality)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return b64, f"data:image/jpeg;base64,{b64}"


def similarity_to_percent_sigmoid(sim: float, mid: float, k: float) -> float:
    """sim suele estar entre ~0.0 y ~1.0 (cosine similarity).

    NO es probabilidad. Esto es un mapeo UI-friendly con curva sigmoid.
    """

    sim = max(0.0, min(1.0, float(sim)))
    x = (sim - mid) * k
    y = 1.0 / (1.0 + math.exp(-x))
    return round(y * 100.0, 2)


def compare_id_against_segments(np_id: np.ndarray, segments: list[dict | None], face_stats: dict) -> dict:
    """Compara la cara de la identificacion contra la mejor cara de cada tramo.

    El porcentaje es el promedio de los tramos, pero solo es match si TODOS
    pasan el umbral: con el promedio solo, un tramo con otra persona quedaria
    escondido (0.90 + 0.90 + 0.10 promedia 0.63 y pasaria).
    """
    id_face = pick_best_face(detect_faces(np_id))
    if id_face is None:
        raise CheckError(422, "id_face_not_found", "No se detectó rostro en la identificación.")

    id_emb = embed(np_id, id_face)
    id_bbox = to_bbox_dict(id_face)
    _, id_face_data_uri = img_to_base64_jpeg(crop_face(np_id, id_bbox))

    threshold = settings.sim_threshold
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
                "face_data_uri": None,
                "live_score": None,
            })
            continue

        sim = cosine_similarity(id_emb, embed(seg["crop"], seg["face"]))
        _, uri = img_to_base64_jpeg(Image.fromarray(seg["crop"]))
        results.append({
            "index": index,
            "time_sec": seg["time_sec"],
            "face_found": True,
            "raw_similarity": round(sim, 6),
            "similarity_percent": similarity_to_percent_sigmoid(sim, threshold, settings.score_k),
            "is_match": sim >= threshold,
            "face_data_uri": uri,
            "live_score": seg["live_score"],
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
        "tolerance": float(threshold),
        "segments": results,
        "multiple_faces": multiple_faces,
        "image1_bbox": id_bbox,
        "image1_face_data_uri": id_face_data_uri,
    }


async def compare_image_np_and_video_path(np1: np.ndarray, video_path: str) -> dict:
    """Compara la identificacion contra la cara de cada tramo del video en disco."""

    try:
        segments, face_stats = await asyncio.wait_for(
            asyncio.to_thread(best_faces_by_segment, video_path, segments=settings.face_segments),
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

    return await asyncio.to_thread(compare_id_against_segments, np1, segments, face_stats)

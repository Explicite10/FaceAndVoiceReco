import asyncio
import time

import cv2
import numpy as np
from fastapi import UploadFile

from app.core.config import settings
from app.services.antispoof import real_probability
from app.services.face_engine import (
    cosine_similarity, detect_faces, embed, pick_best_face
)
from app.services.face_service import (
    ensure_image,
    percent_to_similarity,
    read_as_np_rgb,
    similarity_to_percent_sigmoid,
)
from app.services.image_quality import face_quality
from app.services.report import build_photo_report


def compare_photo_with_id(
    np_id: np.ndarray, np_photo: np.ndarray, threshold_percent: float
) -> dict:
    """Compara la cara mas grande de la foto contra la de la identificacion.

    En la identificacion se toma la cara mas grande (la INE trae una segunda
    foto "fantasma"); en la foto, cualquier cara extra cuenta como otra persona.
    """
    id_face = pick_best_face(detect_faces(np_id))
    photo_faces = detect_faces(np_photo)
    photo_face = pick_best_face(photo_faces)

    similarity = percent = None
    if id_face is not None and photo_face is not None:
        similarity = cosine_similarity(embed(np_id, id_face), embed(np_photo, photo_face))
        percent = similarity_to_percent_sigmoid(similarity, settings.sim_threshold, settings.score_k)

    return {
        "id_face": None if id_face is None else {
            "quality": face_quality(np_id, id_face),
        },
        "photo_face": None if photo_face is None else {
            "quality": face_quality(np_photo, photo_face),
            "real_probability": round(
                real_probability(cv2.cvtColor(np_photo, cv2.COLOR_RGB2BGR), photo_face.bbox), 4
            ),
        },
        "faces_in_photo": len(photo_faces),
        "similarity": None if similarity is None else round(similarity, 6),
        "similarity_percent": percent,
        # Se compara el % que se muestra, para que lo visto y lo decidido coincidan.
        "is_match": percent is not None and percent >= threshold_percent,
        "threshold_percent": float(threshold_percent),
        "threshold_score": percent_to_similarity(threshold_percent, settings.sim_threshold, settings.score_k),
    }


async def verify_photo(
    id_image: UploadFile,
    photo: UploadFile,
    match_threshold_percent: float | None = None,
) -> dict:
    t0 = time.time()
    if match_threshold_percent is None:
        match_threshold_percent = settings.face_match_percent

    ensure_image(id_image, "id_image")
    ensure_image(photo, "photo")
    np_id = await read_as_np_rgb(id_image, "id_image")
    np_photo = await read_as_np_rgb(photo, "photo")

    analysis = await asyncio.to_thread(
        compare_photo_with_id, np_id, np_photo, match_threshold_percent
    )
    return build_photo_report(
        analysis, elapsed_ms=int((time.time() - t0) * 1000)
    )

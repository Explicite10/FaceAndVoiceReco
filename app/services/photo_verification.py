import asyncio
import time

import cv2
import numpy as np
from fastapi import UploadFile

from app.core.config import settings
from app.services.antispoof import real_probability
from app.services.face_engine import (
    Face, cosine_similarity, detect_faces, embed, pick_best_face, to_bbox_dict
)
from app.services.face_service import (
    crop_face,
    ensure_image,
    img_to_base64_jpeg,
    read_as_np_rgb,
    similarity_to_percent_sigmoid,
)
from app.services.image_quality import face_quality
from app.services.report import build_photo_report


def _face_data_uri(np_rgb: np.ndarray, face: Face) -> str:
    _, uri = img_to_base64_jpeg(crop_face(np_rgb, to_bbox_dict(face)))
    return uri


def compare_photo_with_id(np_id: np.ndarray, np_photo: np.ndarray, include_images: bool) -> dict:
    """Compara la cara mas grande de la foto contra la de la identificacion.

    En la identificacion se toma la cara mas grande (la INE trae una segunda
    foto "fantasma"); en la foto, cualquier cara extra cuenta como otra persona.
    """
    id_face = pick_best_face(detect_faces(np_id))
    photo_faces = detect_faces(np_photo)
    photo_face = pick_best_face(photo_faces)

    similarity = None
    if id_face is not None and photo_face is not None:
        similarity = cosine_similarity(embed(np_id, id_face), embed(np_photo, photo_face))

    return {
        "id_face": None if id_face is None else {
            "quality": face_quality(np_id, id_face),
            "data_uri": _face_data_uri(np_id, id_face) if include_images else None,
        },
        "photo_face": None if photo_face is None else {
            "quality": face_quality(np_photo, photo_face),
            "real_probability": round(
                real_probability(cv2.cvtColor(np_photo, cv2.COLOR_RGB2BGR), photo_face.bbox), 4
            ),
            "data_uri": _face_data_uri(np_photo, photo_face) if include_images else None,
        },
        "faces_in_photo": len(photo_faces),
        "similarity": None if similarity is None else round(similarity, 6),
        "similarity_percent": None if similarity is None else similarity_to_percent_sigmoid(
            similarity, settings.sim_threshold, settings.score_k
        ),
    }


async def verify_photo(id_image: UploadFile, photo: UploadFile, include_images: bool = False) -> dict:
    t0 = time.time()

    ensure_image(id_image, "id_image")
    ensure_image(photo, "photo")
    np_id = await read_as_np_rgb(id_image)
    np_photo = await read_as_np_rgb(photo)

    analysis = await asyncio.to_thread(compare_photo_with_id, np_id, np_photo, include_images)
    return build_photo_report(
        analysis, elapsed_ms=int((time.time() - t0) * 1000), include_images=include_images
    )

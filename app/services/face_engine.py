"""Deteccion y reconocimiento facial con modelos de OpenCV Zoo.

YuNet (MIT) detecta; SFace (Apache 2.0) genera el embedding. Ambos permiten
uso comercial, a diferencia de los modelos preentrenados de InsightFace.
"""
import logging
import threading
from dataclasses import dataclass

import cv2
import numpy as np

from app.services.model_store import ensure_model

logger = logging.getLogger("uvicorn.error")

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
_MODELS = {
    "detector": (
        "face_detection_yunet_2023mar.onnx",
        f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
    ),
    "recognizer": (
        "face_recognition_sface_2021dec.onnx",
        f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
    ),
}

# YuNet esta entrenado para caras de ~10 a ~300 px: si a 640 no encuentra nada
# (cara muy pegada a la camara) se reintenta a menor escala.
_DETECT_LONG_SIDES = (640, 320)
_DETECT_SCORE = 0.6

_detector = None
_recognizer = None
# Los objetos de cv2 guardan estado (setInputSize) y no son seguros entre hilos.
_lock = threading.Lock()


@dataclass
class Face:
    bbox: np.ndarray   # x1, y1, x2, y2 en la imagen original
    score: float
    row: np.ndarray    # fila YuNet (caja + 5 puntos + score) en coords originales


def load_face_models() -> None:
    global _detector, _recognizer
    with _lock:
        if _detector is not None:
            return
        det_path = ensure_model(*_MODELS["detector"])
        rec_path = ensure_model(*_MODELS["recognizer"])
        _detector = cv2.FaceDetectorYN.create(det_path, "", (640, 640), _DETECT_SCORE, 0.3, 5000)
        _recognizer = cv2.FaceRecognizerSF.create(rec_path, "")
        logger.info("Rostro: YuNet + SFace listos")


def detect_faces(np_rgb: np.ndarray) -> list[Face]:
    if _detector is None:
        load_face_models()

    bgr = cv2.cvtColor(np_rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    tried: set[float] = set()

    for long_side in _DETECT_LONG_SIDES:
        scale = min(1.0, long_side / max(h, w))
        if scale in tried:
            continue
        tried.add(scale)

        img = bgr if scale == 1.0 else cv2.resize(
            bgr, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA
        )
        with _lock:
            _detector.setInputSize((img.shape[1], img.shape[0]))
            _, rows = _detector.detect(img)

        if rows is not None and len(rows):
            rows = rows.astype(np.float32).copy()
            rows[:, :14] /= scale
            return [
                Face(
                    bbox=np.array([r[0], r[1], r[0] + r[2], r[1] + r[3]], dtype=np.float32),
                    score=float(r[14]),
                    row=r,
                )
                for r in rows
            ]
    return []


def embed(np_rgb: np.ndarray, face: Face) -> np.ndarray:
    """Embedding normalizado (128 valores) de la cara alineada por sus 5 puntos."""
    bgr = cv2.cvtColor(np_rgb, cv2.COLOR_RGB2BGR)
    with _lock:
        aligned = _recognizer.alignCrop(bgr, face.row.reshape(1, -1))
        feat = _recognizer.feature(aligned).reshape(-1).astype(np.float32)
    return feat / (np.linalg.norm(feat) + 1e-12)


def pick_best_face(faces):
    if not faces:
        return None

    def area(face):
        x1, y1, x2, y2 = face.bbox.astype(int)
        return max(0, x2 - x1) * max(0, y2 - y1)

    return max(faces, key=area)


def to_bbox_dict(face) -> dict:
    x1, y1, x2, y2 = face.bbox.astype(int)
    return {"top": int(y1), "right": int(x2), "bottom": int(y2), "left": int(x1)}


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    a = a.astype("float32")
    b = b.astype("float32")
    a = a / (np.linalg.norm(a) + 1e-12)
    b = b / (np.linalg.norm(b) + 1e-12)
    return float(np.dot(a, b))

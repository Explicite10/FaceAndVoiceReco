import cv2
import numpy as np

from app.services.face_engine import Face


def sharpness(gray_face: np.ndarray) -> float:
    # A tamano fijo para que caras grandes y chicas sean comparables.
    return float(cv2.Laplacian(cv2.resize(gray_face, (112, 112)), cv2.CV_64F).var())


def face_quality(np_rgb: np.ndarray, face: Face) -> dict:
    """Tamano (lado menor en px), nitidez, brillo medio (0-255) y confianza del detector."""
    h, w = np_rgb.shape[:2]
    x1, y1, x2, y2 = face.bbox.astype(int)
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 <= x1 or y2 <= y1:
        return {"size_px": 0, "sharpness": 0.0, "brightness": 0.0, "detector_score": round(face.score, 4)}

    gray = cv2.cvtColor(np_rgb[y1:y2, x1:x2], cv2.COLOR_RGB2GRAY)
    return {
        "size_px": int(min(x2 - x1, y2 - y1)),
        "sharpness": round(sharpness(gray), 2),
        "brightness": round(float(gray.mean()), 2),
        "detector_score": round(face.score, 4),
    }

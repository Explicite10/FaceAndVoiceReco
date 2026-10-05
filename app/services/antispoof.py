"""Deteccion de suplantacion (pantallas, fotos impresas, mascaras) con MiniFASNet.

Modelos de Silent-Face-Anti-Spoofing (Apache 2.0) convertidos a ONNX; ver
app/assets/antispoof/NOTICE.
"""
import os
import threading

import cv2
import numpy as np
import onnxruntime as ort

_ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "antispoof")
# Cada modelo mira la cara con distinto contexto: 4.0x alcanza a ver bordes de pantalla.
_MODELS = (("2.7_80x80_MiniFASNetV2.onnx", 2.7), ("4_0_0_80x80_MiniFASNetV1SE.onnx", 4.0))
_INPUT = 80
_REAL_CLASS = 1

_sessions: list[tuple[ort.InferenceSession, float]] | None = None
_lock = threading.Lock()


def load_antispoof_models() -> None:
    global _sessions
    with _lock:
        if _sessions is None:
            _sessions = [
                (ort.InferenceSession(os.path.join(_ASSETS, name), providers=["CPUExecutionProvider"]), scale)
                for name, scale in _MODELS
            ]


def _crop(bgr: np.ndarray, box_xywh: list[int], scale: float) -> np.ndarray:
    # Replica CropImage de Silent-Face: el recorte se desplaza para no salirse del cuadro.
    src_h, src_w = bgr.shape[:2]
    x, y, box_w, box_h = box_xywh
    scale = min((src_h - 1) / box_h, (src_w - 1) / box_w, scale)
    new_w, new_h = box_w * scale, box_h * scale
    cx, cy = x + box_w / 2, y + box_h / 2

    x1, y1, x2, y2 = cx - new_w / 2, cy - new_h / 2, cx + new_w / 2, cy + new_h / 2
    if x1 < 0:
        x2 -= x1
        x1 = 0
    if y1 < 0:
        y2 -= y1
        y1 = 0
    if x2 > src_w - 1:
        x1 -= x2 - src_w + 1
        x2 = src_w - 1
    if y2 > src_h - 1:
        y1 -= y2 - src_h + 1
        y2 = src_h - 1

    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    return cv2.resize(bgr[y1:y2 + 1, x1:x2 + 1], (_INPUT, _INPUT))


def real_probability(bgr: np.ndarray, bbox_xyxy: np.ndarray) -> float:
    """Probabilidad (0-1) de que la cara sea real y no una pantalla, foto o mascara."""
    if _sessions is None:
        load_antispoof_models()

    x1, y1, x2, y2 = [int(v) for v in bbox_xyxy]
    box = [max(0, x1), max(0, y1), max(1, x2 - x1 + 1), max(1, y2 - y1 + 1)]

    total = np.zeros(3, dtype=np.float64)
    for session, scale in _sessions:
        # El modelo espera BGR en rango 0-255 (Silent-Face no normaliza a 0-1).
        patch = _crop(bgr, box, scale).astype(np.float32).transpose(2, 0, 1)[None]
        logits = session.run(None, {"input": patch})[0][0].astype(np.float64)
        e = np.exp(logits - logits.max())
        total += e / e.sum()
    return float(total[_REAL_CLASS] / len(_sessions))

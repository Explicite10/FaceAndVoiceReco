"""Evidencia del intento final para el proyecto: el video y algunos fotogramas.

Solo si el proyecto la pide al crear la sesion. No se guarda en disco ni en la
base: vive en memoria hasta que el webhook confirma la entrega (o vence la sesion).
"""
import base64
import os

import cv2

FOTOGRAMAS = 5
CALIDAD_JPEG = 85
_TIPOS_VIDEO = {".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}


def _b64(datos: bytes) -> str:
    return base64.b64encode(datos).decode("ascii")


def leer_video(path: str) -> dict:
    with open(path, "rb") as f:
        datos = f.read()
    tipo = _TIPOS_VIDEO.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    return {"tipo": tipo, "bytes": len(datos), "base64": _b64(datos)}


def extraer_fotogramas(path: str, cantidad: int = FOTOGRAMAS) -> list[dict]:
    """Cuadros completos repartidos a lo largo del video, en JPEG.

    Se cuentan los cuadros leyendo el video: los .webm de Chrome no traen la
    duracion en los metadatos, asi que CAP_PROP_FRAME_COUNT no sirve.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    try:
        total = 0
        while cap.grab():
            total += 1
    finally:
        cap.release()
    if total == 0:
        return []

    # Centro de cada tramo: evita el primer cuadro (suele salir oscuro) y el ultimo.
    elegidos = sorted({min(total - 1, int(total * (k + 0.5) / cantidad)) for k in range(cantidad)})
    fotogramas = []
    cap = cv2.VideoCapture(path)
    try:
        indice = -1
        for objetivo in elegidos:
            while indice < objetivo:
                if not cap.grab():
                    return fotogramas
                indice += 1
            ok, cuadro = cap.retrieve()
            if not ok:
                continue
            ok, jpeg = cv2.imencode(".jpg", cuadro, [cv2.IMWRITE_JPEG_QUALITY, CALIDAD_JPEG])
            if ok:
                fotogramas.append({
                    "segundo": round(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0, 2),
                    "tipo": "image/jpeg",
                    "base64": _b64(jpeg.tobytes()),
                })
    finally:
        cap.release()
    return fotogramas


def armar(path: str, intento: int, video: bool, fotogramas: bool) -> dict:
    return {
        "intento": intento,
        "video": leer_video(path) if video else None,
        "fotogramas": extraer_fotogramas(path) if fotogramas else [],
    }

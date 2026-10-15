"""Evidencia del intento final para el proyecto: el video y algunos fotogramas.

Solo si el proyecto la pide al crear la sesion. No se guarda en disco ni en la
base: vive en memoria hasta que el webhook confirma la entrega (o vence la sesion).
Viaja en binario, como archivos de un webhook multipart (ver entrega.py).
"""
import hashlib
import os

import cv2

FOTOGRAMAS = 5
CALIDAD_JPEG = 85
_TIPOS_VIDEO = {".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}
_EXTENSIONES = {"video/webm": "webm", "video/mp4": "mp4", "video/quicktime": "mov", "video/x-matroska": "mkv",
                "video/x-msvideo": "avi", "image/jpeg": "jpg"}


def _archivo(campo: str, tipo: str, datos: bytes, **extra) -> dict:
    return {"campo": campo, "nombre": f"{campo}.{_EXTENSIONES.get(tipo, 'bin')}", "tipo": tipo, "datos": datos, **extra}


def leer_video(path: str) -> dict:
    with open(path, "rb") as f:
        datos = f.read()
    tipo = _TIPOS_VIDEO.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    return _archivo("video", tipo, datos)


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
                segundo = round(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0, 2)
                fotogramas.append(_archivo(f"fotograma_{len(fotogramas) + 1}", "image/jpeg", jpeg.tobytes(),
                                           segundo=segundo))
    finally:
        cap.release()
    return fotogramas


def armar(path: str, video: bool, fotogramas: bool) -> list[dict]:
    """Archivos a entregar: [{campo, nombre, tipo, datos, segundo?}]."""
    return ([leer_video(path)] if video else []) + (extraer_fotogramas(path) if fotogramas else [])


def describir(intento: int, archivos: list[dict]) -> dict:
    """Lo que va en el JSON firmado: sin los bytes, con su SHA-256 para comprobarlos."""
    return {
        "intento": intento,
        "archivos": [
            {**{k: v for k, v in a.items() if k != "datos"}, "bytes": len(a["datos"]),
             "sha256": hashlib.sha256(a["datos"]).hexdigest()}
            for a in archivos
        ],
    }

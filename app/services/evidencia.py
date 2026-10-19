"""Evidencia del intento final para el proyecto: video, fotogramas y cara de la INE.

Solo si el proyecto la pide al crear la sesion. El webhook trae ligas temporales
y el proyecto descarga los archivos con su X-API-Key. Viven en un directorio
temporal del contenedor hasta que vencen (EVIDENCE_MINUTES) o el proyecto los
borra; al reiniciar el servicio se borran todos. Nunca pasan a la base.
"""
import logging
import os
import shutil
import subprocess

import cv2
import numpy as np

from app.services.face_engine import MAX_GIRO_FRENTE, detect_faces, pick_best_face, pose
from app.services.image_quality import sharpness

logger = logging.getLogger("uvicorn.error")

FOTOGRAMAS = 3
CALIDAD_JPEG = 90
# Para la foto se pide algo mas derecho que para comparar con la INE.
_MAX_INCLINACION = 0.15
_PASO = 3  # se revisa uno de cada 3 cuadros (~10 por segundo)
_TIPOS_VIDEO = {".webm": "video/webm", ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}


def directorio(base: str, sesion_id: str) -> str:
    return os.path.join(base, "evidencia", sesion_id)


def borrar(base: str, sesion_id: str) -> None:
    shutil.rmtree(directorio(base, sesion_id), ignore_errors=True)


def borrar_todo(base: str) -> None:
    shutil.rmtree(os.path.join(base, "evidencia"), ignore_errors=True)


def _jpeg(np_bgr: np.ndarray) -> bytes:
    ok, datos = cv2.imencode(".jpg", np_bgr, [cv2.IMWRITE_JPEG_QUALITY, CALIDAD_JPEG])
    if not ok:
        raise ValueError("No se pudo codificar el JPEG")
    return datos.tobytes()


def a_mp4(origen: str, destino: str) -> bool:
    """H.264 + AAC con faststart, lo que reproduce cualquier navegador o servicio."""
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", origen,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", destino],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("No se pudo convertir el video a mp4: %s", exc)
        return False
    if r.returncode != 0:
        logger.warning("ffmpeg fallo al convertir a mp4: %s", r.stderr.decode(errors="replace")[:300])
        return False
    return True


def fotogramas_de_frente(path: str, cantidad: int = FOTOGRAMAS) -> list[dict]:
    """El mejor cuadro de frente de cada tramo del video, completo y en JPEG.

    En cada tramo gana el cuadro con una sola cara mas de frente y derecha, entre
    los razonablemente nitidos; si en un tramo nunca miro de frente (estaba
    haciendo un gesto), el menos girado.
    """
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return []
    candidatos = []  # (indice, de_frente, giro, nitidez, segundo, jpeg, inclinacion)
    indice = 0
    try:
        while cap.grab():
            indice += 1
            if indice % _PASO:
                continue
            ok, cuadro = cap.retrieve()
            if not ok:
                continue
            caras = detect_faces(cv2.cvtColor(cuadro, cv2.COLOR_BGR2RGB))
            if len(caras) != 1:
                continue
            cara = caras[0]
            giro, inclinacion = pose(cara.row)
            x1, y1, x2, y2 = (int(v) for v in cara.bbox)
            gris = cv2.cvtColor(cuadro[max(0, y1):max(0, y2), max(0, x1):max(0, x2)], cv2.COLOR_BGR2GRAY)
            if gris.size == 0:
                continue
            candidatos.append((indice, giro <= MAX_GIRO_FRENTE and inclinacion <= _MAX_INCLINACION, giro,
                               sharpness(gris) * cara.score, round(cap.get(cv2.CAP_PROP_POS_MSEC) / 1000, 2),
                               _jpeg(cuadro), inclinacion))
    finally:
        cap.release()

    elegidos = []
    for k in range(cantidad):
        lo, hi = indice * k / cantidad, indice * (k + 1) / cantidad
        tramo = [c for c in candidatos if lo < c[0] <= hi]
        if not tramo:
            continue
        de_frente = [c for c in tramo if c[1]]
        if de_frente:
            # El mas derecho de los que estan razonablemente nitidos.
            nitidez = max(c[3] for c in de_frente)
            mejor = min((c for c in de_frente if c[3] >= nitidez * 0.5), key=lambda c: c[2] + c[6])
        else:
            mejor = min(tramo, key=lambda c: c[2])
        elegidos.append({"segundo": mejor[4], "de_frente": mejor[1], "datos": mejor[5]})
    return elegidos


def recorte_rostro(np_rgb: np.ndarray, margen: float = 0.3) -> bytes | None:
    """La cara de la INE con un margen alrededor, en JPEG. None si no hay cara."""
    cara = pick_best_face(detect_faces(np_rgb))
    if cara is None:
        return None
    h, w = np_rgb.shape[:2]
    x1, y1, x2, y2 = (float(v) for v in cara.bbox)
    mx, my = (x2 - x1) * margen, (y2 - y1) * margen
    x1, y1 = max(0, int(x1 - mx)), max(0, int(y1 - my))
    x2, y2 = min(w, int(x2 + mx)), min(h, int(y2 + my))
    if x2 <= x1 or y2 <= y1:
        return None
    return _jpeg(cv2.cvtColor(np_rgb[y1:y2, x1:x2], cv2.COLOR_RGB2BGR))


def guardar(base: str, sesion_id: str, video_path: str, video: bool, fotogramas: bool,
            rostro_ine: bytes | None) -> dict:
    """Escribe la evidencia y devuelve {nombre: {archivo, tipo, ...}} para servirla.

    nombres: "video", "fotograma/1".., "rostro-ine". El video sale en mp4; si la
    conversion falla se entrega el original con su tipo (webm, por ejemplo).
    """
    carpeta = directorio(base, sesion_id)
    os.makedirs(carpeta, exist_ok=True)
    archivos: dict[str, dict] = {}

    if video:
        destino = os.path.join(carpeta, "video.mp4")
        if a_mp4(video_path, destino):
            archivos["video"] = {"archivo": destino, "tipo": "video/mp4"}
        else:
            ext = os.path.splitext(video_path)[1].lower()
            destino = os.path.join(carpeta, "video" + ext)
            shutil.copyfile(video_path, destino)
            archivos["video"] = {"archivo": destino, "tipo": _TIPOS_VIDEO.get(ext, "application/octet-stream")}

    if fotogramas:
        for orden, f in enumerate(fotogramas_de_frente(video_path), start=1):
            destino = os.path.join(carpeta, f"fotograma_{orden}.jpg")
            with open(destino, "wb") as fh:
                fh.write(f["datos"])
            archivos[f"fotograma/{orden}"] = {"archivo": destino, "tipo": "image/jpeg", "orden": orden,
                                              "segundo": f["segundo"], "de_frente": f["de_frente"]}

    if rostro_ine:
        destino = os.path.join(carpeta, "rostro_ine.jpg")
        with open(destino, "wb") as fh:
            fh.write(rostro_ine)
        archivos["rostro-ine"] = {"archivo": destino, "tipo": "image/jpeg"}

    for a in archivos.values():
        a["bytes"] = os.path.getsize(a["archivo"])
    return archivos

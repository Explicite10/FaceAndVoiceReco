"""Orientacion del video: que la cara llegue derecha al analisis.

Algunos celulares (sobre todo iPhone desde el navegador) graban la imagen "acostada",
como esta montado el sensor, y no siempre guardan la etiqueta de rotacion. OpenCV ya
respeta esa etiqueta cuando existe; esto cubre el caso en que falta: se revisan unos
cuantos cuadros en 0/90/180/270 grados y se usa el giro donde la cara se detecta mejor.

La pagina de la prueba ya graba los cuadros derechos; esto es el respaldo del servidor
(videos subidos, navegadores raros).
"""
import logging
import os

import cv2

from app.services.face_engine import detect_faces, pick_best_face

logger = logging.getLogger("uvicorn.error")

_GIROS = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
_MUESTRAS = 8           # cuadros que se revisan
_CADA = 10              # uno de cada N cuadros leidos
# Otro giro solo gana si es claramente mejor que dejarlo como viene.
_VENTAJA = 1.5

_cache: dict[tuple, int] = {}


def girar(frame, angulo: int):
    """El cuadro girado `angulo` grados en sentido horario (0 = sin cambio)."""
    return frame if not angulo else cv2.rotate(frame, _GIROS[angulo])


def _puntaje(frame_bgr) -> float:
    cara = pick_best_face(detect_faces(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)))
    return float(cara.score) if cara is not None else 0.0


def orientacion(video_path: str) -> int:
    """Grados (0, 90, 180, 270) a girar cada cuadro para que la cara quede derecha."""
    try:
        st = os.stat(video_path)
        clave = (video_path, st.st_size, st.st_mtime_ns)
    except OSError:
        return 0
    if clave in _cache:
        return _cache[clave]

    puntajes = {0: 0.0, 90: 0.0, 180: 0.0, 270: 0.0}
    cap = cv2.VideoCapture(video_path)
    leidos = revisados = 0
    try:
        while revisados < _MUESTRAS:
            ok, frame = cap.read()
            if not ok:
                break
            leidos += 1
            if leidos % _CADA:
                continue
            revisados += 1
            for angulo in puntajes:
                puntajes[angulo] += _puntaje(girar(frame, angulo))
    finally:
        cap.release()

    mejor = max(puntajes, key=puntajes.get)
    angulo = mejor if mejor and puntajes[mejor] > _VENTAJA * puntajes[0] else 0
    if angulo:
        logger.info("Video girado: se corrige %s grados (puntajes %s)", angulo,
                    {k: round(v, 2) for k, v in puntajes.items()})
    if len(_cache) > 64:
        _cache.clear()
    _cache[clave] = angulo
    return angulo

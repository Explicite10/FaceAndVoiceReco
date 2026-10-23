"""Una sola pasada de MediaPipe por el video para labios y gestos del reto.

Face Landmarker (Apache 2.0) da la apertura de la boca, el parpadeo
(blendshapes) y la posicion de la nariz; Hand Landmarker (Apache 2.0) da las
manos. Las coordenadas son las del cuadro tal como lo grabo la camara, sin
espejo: el lado derecho del usuario queda a la izquierda de la imagen.
"""
import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import BaseOptions, vision

from app.services.model_store import ensure_model
from app.services.orientacion import girar, orientacion

FACE_LANDMARKER = (
    "face_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
)
HAND_LANDMARKER = (
    "hand_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
    "fbc2a30080c3c557093b5ddfc334698132eb341044ccee322ccf8bcf3607cde1",
)
# Puntos de Face Mesh: labios internos, eje frente-barbilla, punta de la nariz
# y bordes de la cara a la altura de los pomulos.
_UPPER_LIP, _LOWER_LIP, _FOREHEAD, _CHIN = 13, 14, 10, 152
_NOSE_TIP, _FACE_EDGE_A, _FACE_EDGE_B = 1, 234, 454
# Centro de la palma: nudillo del dedo medio.
_PALM = 9
TRACK_FPS = 15.0
# Las manos se buscan en uno de cada N cuadros: el modelo es mas caro y un
# brazo alzado dura mucho mas que un parpadeo.
_HAND_EVERY = 2


def load_track_models() -> None:
    ensure_model(*FACE_LANDMARKER)
    ensure_model(*HAND_LANDMARKER)


def _options(cls, path: str, **kw):
    # Solo CPU: en macOS el delegado por defecto intenta Metal y, fuera del hilo
    # principal, aborta el proceso entero con un error nativo no capturable.
    return cls(
        base_options=BaseOptions(model_asset_path=path, delegate=BaseOptions.Delegate.CPU),
        running_mode=vision.RunningMode.VIDEO,
        **kw,
    )


def track_video(video_path: str, with_hands: bool) -> dict:
    """Series por cuadro (~15 fps). NaN donde no hubo cara.

    - aperture: apertura de la boca / alto de la cara
    - blink: promedio de eyeBlinkLeft y eyeBlinkRight (0 abiertos, 1 cerrados)
    - turn: (nariz - centro de la cara) / ancho de la cara, en x de la imagen;
      positivo = nariz hacia la derecha de la imagen = el usuario gira a SU izquierda
    - nose_x, chin_y: normalizados 0-1, para ubicar las manos respecto a la cara
    - hands: lista de (x, y) del centro de cada palma, o None si ese cuadro no se reviso
    """
    face_lm = vision.FaceLandmarker.create_from_options(
        _options(vision.FaceLandmarkerOptions, ensure_model(*FACE_LANDMARKER), num_faces=1, output_face_blendshapes=True)
    )
    hand_lm = (
        vision.HandLandmarker.create_from_options(
            _options(vision.HandLandmarkerOptions, ensure_model(*HAND_LANDMARKER), num_hands=2)
        )
        if with_hands else None
    )
    series = {k: [] for k in ("times", "aperture", "blink", "turn", "nose_x", "chin_y", "hands")}
    angulo = orientacion(video_path)  # endereza videos grabados "acostados"
    cap = cv2.VideoCapture(video_path)
    last_t, last_ms, n = -1e9, -1, 0
    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if t - last_t < 1.0 / TRACK_FPS - 1e-3:
                continue
            last_t = t
            # El modo VIDEO exige marcas de tiempo estrictamente crecientes.
            last_ms = max(int(t * 1000), last_ms + 1)

            rgb = cv2.cvtColor(girar(frame_bgr, angulo), cv2.COLOR_BGR2RGB)
            image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            face = face_lm.detect_for_video(image, last_ms)

            aperture = blink = turn = nose_x = chin_y = np.nan
            if face.face_landmarks:
                pts = face.face_landmarks[0]
                h, w = rgb.shape[:2]

                def px(i):
                    return np.array([pts[i].x * w, pts[i].y * h])

                face_h = float(np.linalg.norm(px(_FOREHEAD) - px(_CHIN)))
                if face_h > 1.0:
                    aperture = float(np.linalg.norm(px(_UPPER_LIP) - px(_LOWER_LIP))) / face_h

                edge_a, edge_b = pts[_FACE_EDGE_A].x, pts[_FACE_EDGE_B].x
                face_w = abs(edge_b - edge_a)
                if face_w > 1e-3:
                    turn = (pts[_NOSE_TIP].x - (edge_a + edge_b) / 2) / face_w
                nose_x, chin_y = pts[_NOSE_TIP].x, pts[_CHIN].y

                if face.face_blendshapes:
                    scores = {c.category_name: c.score for c in face.face_blendshapes[0]}
                    blink = (scores.get("eyeBlinkLeft", 0.0) + scores.get("eyeBlinkRight", 0.0)) / 2

            hands = None
            if hand_lm is not None and n % _HAND_EVERY == 0:
                found = hand_lm.detect_for_video(image, last_ms)
                hands = [(h[_PALM].x, h[_PALM].y) for h in found.hand_landmarks]
            n += 1

            series["times"].append(t)
            series["aperture"].append(aperture)
            series["blink"].append(blink)
            series["turn"].append(turn)
            series["nose_x"].append(nose_x)
            series["chin_y"].append(chin_y)
            series["hands"].append(hands)
    finally:
        cap.release()
        face_lm.close()
        if hand_lm is not None:
            hand_lm.close()

    return {k: (v if k == "hands" else np.array(v, dtype=float)) for k, v in series.items()}

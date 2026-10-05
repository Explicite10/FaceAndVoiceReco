import cv2
import numpy as np

from app.services.antispoof import real_probability
from app.services.face_engine import Face, detect_faces, pick_best_face

# Los candidatos se guardan reducidos para acotar memoria; SFace trabaja a 112 px.
_MAX_STORED_SIDE = 256


def _sharpness(gray_face: np.ndarray) -> float:
    # A tamano fijo para que caras grandes y chicas sean comparables.
    return float(cv2.Laplacian(cv2.resize(gray_face, (112, 112)), cv2.CV_64F).var())


def best_faces_by_segment(
    video_path: str,
    segments: int = 3,
    frame_step: int = 5,
    max_frames: int = 300,
    min_face_area: int = 60 * 60,
    padding_ratio: float = 0.30,
) -> tuple[list[dict | None], dict]:
    """Divide el video en tramos de igual duracion y devuelve la cara mas nitida de cada uno.

    Cada elemento es {"crop", "face", "time_sec", "live_score"} o None si en ese
    tramo no hubo cara. `live_score` promedia el detector de suplantacion sobre
    todos los cuadros con cara del tramo, no solo el elegido: es mas estable.
    `face` viene en coordenadas del recorte, lista para embed() sin volver a detectar.
    Los tramos se calculan con los cuadros realmente leidos: los .webm de Chrome
    suelen no traer la duracion en los metadatos.

    Tambien devuelve cuantas caras hubo por cuadro, para detectar a otra persona
    en el video (solo se usa la mas grande para comparar).
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return [None] * segments, {"max_faces_in_frame": 0, "multi_face_frames": 0, "first_multi_face_sec": None}

    candidates: list[tuple[int, float, float, dict]] = []
    frame_index = 0
    processed = 0
    max_faces = 0
    multi_face_frames = 0
    first_multi_face_sec = None

    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break

            frame_index += 1
            if frame_index % frame_step != 0:
                continue

            processed += 1
            if processed > max_frames:
                break

            time_sec = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            faces = detect_faces(frame_rgb)
            max_faces = max(max_faces, len(faces))
            if len(faces) > 1:
                multi_face_frames += 1
                if first_multi_face_sec is None:
                    first_multi_face_sec = round(time_sec, 2)

            face = pick_best_face(faces)
            if face is None:
                continue

            h_img, w_img = frame_rgb.shape[:2]
            x1, y1, x2, y2 = [int(v) for v in face.bbox]
            pad_x = int(max(0, x2 - x1) * padding_ratio)
            pad_y = int(max(0, y2 - y1) * padding_ratio)
            cx1, cy1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
            cx2, cy2 = min(w_img, x2 + pad_x), min(h_img, y2 + pad_y)

            if (cx2 - cx1) * (cy2 - cy1) < min_face_area:
                continue

            face_gray = cv2.cvtColor(
                frame_bgr[max(0, y1):max(0, y2), max(0, x1):max(0, x2)], cv2.COLOR_BGR2GRAY
            )
            if face_gray.size == 0:
                continue
            quality = face.score * _sharpness(face_gray)
            live = real_probability(frame_bgr, face.bbox)

            crop = frame_rgb[cy1:cy2, cx1:cx2]
            row = face.row.copy()
            # Fila YuNet: x, y, w, h y luego 5 puntos (x, y). w y h no se desplazan.
            row[0] -= cx1
            row[1] -= cy1
            row[4:14:2] -= cx1
            row[5:14:2] -= cy1

            scale = min(1.0, _MAX_STORED_SIDE / max(crop.shape[:2]))
            if scale < 1.0:
                crop = cv2.resize(
                    crop,
                    (max(1, round(crop.shape[1] * scale)), max(1, round(crop.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )
                row[:14] *= scale
            else:
                crop = crop.copy()

            local = Face(
                bbox=np.array([row[0], row[1], row[0] + row[2], row[1] + row[3]], dtype=np.float32),
                score=face.score,
                row=row,
            )
            candidates.append((frame_index, quality, live, {"crop": crop, "face": local, "time_sec": round(time_sec, 2)}))
    finally:
        cap.release()

    total = frame_index
    result: list[dict | None] = []
    for k in range(segments):
        lo, hi = total * k / segments, total * (k + 1) / segments
        inside = [c for c in candidates if lo < c[0] <= hi]
        if not inside:
            result.append(None)
            continue
        best = dict(max(inside, key=lambda c: c[1])[3])
        best["live_score"] = round(float(np.mean([c[2] for c in inside])), 4)
        result.append(best)

    stats = {
        "max_faces_in_frame": max_faces,
        "multi_face_frames": multi_face_frames,
        "first_multi_face_sec": first_multi_face_sec,
    }
    return result, stats

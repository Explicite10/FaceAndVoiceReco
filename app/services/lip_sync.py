"""Sincronia labios-voz: la boca debe moverse cuando suena la voz.

Usa MediaPipe Face Landmarker (Apache 2.0) para medir la apertura de la boca
cuadro por cuadro y la correlaciona con la energia del audio.
"""
import cv2
import mediapipe as mp
import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps
from mediapipe.tasks.python import BaseOptions, vision

from app.core.config import settings
from app.services.audio_extract import SAMPLE_RATE, AudioExtractionError, extract_audio_16k_mono
from app.services.model_store import ensure_model

_LANDMARKER = (
    "face_landmarker.task",
    "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task",
    "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff",
)
# Puntos de Face Mesh: centro de los labios internos y eje frente-barbilla.
_UPPER_LIP, _LOWER_LIP, _FOREHEAD, _CHIN = 13, 14, 10, 152
_TRACK_FPS = 15.0
_ENVELOPE_WIN_SEC = 0.05
# La boca suele moverse un poco antes que el sonido; se busca el mejor desfase.
_MAX_LAG_SEC = 0.2


def load_lip_sync_model() -> None:
    ensure_model(*_LANDMARKER)


def _mouth_track(video_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Tiempos (s) y apertura de boca / alto de cara por cuadro (NaN si no hay cara)."""
    options = vision.FaceLandmarkerOptions(
        # Solo CPU: en macOS el delegado por defecto intenta Metal y, fuera del hilo
        # principal, aborta el proceso entero con un error nativo no capturable.
        base_options=BaseOptions(model_asset_path=ensure_model(*_LANDMARKER), delegate=BaseOptions.Delegate.CPU),
        running_mode=vision.RunningMode.VIDEO,
        num_faces=1,
    )
    times: list[float] = []
    apertures: list[float] = []
    landmarker = vision.FaceLandmarker.create_from_options(options)
    cap = cv2.VideoCapture(video_path)
    last_t, last_ms = -1e9, -1
    try:
        while True:
            ok, frame_bgr = cap.read()
            if not ok:
                break
            t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
            if t - last_t < 1.0 / _TRACK_FPS - 1e-3:
                continue
            last_t = t
            # El modo VIDEO exige marcas de tiempo estrictamente crecientes.
            last_ms = max(int(t * 1000), last_ms + 1)

            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            result = landmarker.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), last_ms)

            aperture = np.nan
            if result.face_landmarks:
                pts = result.face_landmarks[0]
                h, w = rgb.shape[:2]

                def px(i):
                    return np.array([pts[i].x * w, pts[i].y * h])

                face_h = float(np.linalg.norm(px(_FOREHEAD) - px(_CHIN)))
                if face_h > 1.0:
                    aperture = float(np.linalg.norm(px(_UPPER_LIP) - px(_LOWER_LIP))) / face_h

            times.append(t)
            apertures.append(aperture)
    finally:
        cap.release()
        landmarker.close()

    return np.array(times), np.array(apertures)


def _envelope(audio: np.ndarray, times: np.ndarray) -> np.ndarray:
    half = int(_ENVELOPE_WIN_SEC * SAMPLE_RATE / 2)
    out = np.zeros(len(times))
    for i, t in enumerate(times):
        c = int(t * SAMPLE_RATE)
        seg = audio[max(0, c - half): c + half]
        out[i] = np.sqrt(np.mean(seg ** 2)) if seg.size else 0.0
    return np.log(out + 1e-4)


def _speech_mask(audio: np.ndarray, times: np.ndarray) -> np.ndarray:
    chunks = get_speech_timestamps(
        audio, VadOptions(min_silence_duration_ms=200, speech_pad_ms=100), sampling_rate=SAMPLE_RATE
    )
    mask = np.zeros(len(times), dtype=bool)
    for c in chunks:
        mask |= (times >= c["start"] / SAMPLE_RATE) & (times <= c["end"] / SAMPLE_RATE)
    return mask


def _best_lag_correlation(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[float, int]:
    best_r, best_k = -1.0, 0
    for k in range(-max_lag, max_lag + 1):
        x, y = (a[: len(a) - k], b[k:]) if k >= 0 else (a[-k:], b[: len(b) + k])
        ok = ~(np.isnan(x) | np.isnan(y))
        if ok.sum() < 10 or np.std(x[ok]) < 1e-9 or np.std(y[ok]) < 1e-9:
            continue
        r = float(np.corrcoef(x[ok], y[ok])[0, 1])
        if r > best_r:
            best_r, best_k = r, k
    return best_r, best_k


def _mean_motion(ap: np.ndarray, mask: np.ndarray) -> float | None:
    diffs = np.abs(np.diff(ap))
    sel = mask[1:] & mask[:-1] & ~np.isnan(diffs)
    return float(diffs[sel].mean()) if sel.sum() >= 5 else None


def analyze_lip_sync(video_path: str) -> dict:
    result = {
        "passed": False,
        "reason": None,
        "reason_code": None,
        "tracked_ratio": 0.0,
        "mouth_motion_speech": None,
        "mouth_motion_silence": None,
        "correlation": None,
        "lag_ms": None,
    }

    try:
        audio = extract_audio_16k_mono(video_path)
    except AudioExtractionError:
        result["reason"], result["reason_code"] = "el video no tiene audio", "no_audio"
        return result

    times, ap = _mouth_track(video_path)
    if len(times) == 0:
        result["reason"], result["reason_code"] = "no se pudo leer el video", "unreadable_video"
        return result

    tracked = ~np.isnan(ap)
    result["tracked_ratio"] = round(float(tracked.mean()), 3)
    speech = _speech_mask(audio, times)
    motion_speech = _mean_motion(ap, speech)
    motion_silence = _mean_motion(ap, ~speech)
    corr, lag = _best_lag_correlation(ap, _envelope(audio, times), int(round(_MAX_LAG_SEC * _TRACK_FPS)))

    result.update(
        mouth_motion_speech=None if motion_speech is None else round(motion_speech, 5),
        mouth_motion_silence=None if motion_silence is None else round(motion_silence, 5),
        correlation=round(corr, 3) if corr > -1.0 else None,
        lag_ms=int(round(lag * 1000 / _TRACK_FPS)) if corr > -1.0 else None,
    )

    if result["tracked_ratio"] < settings.lip_min_tracked_ratio:
        result["reason"], result["reason_code"] = "no se ve la cara la mayor parte del video", "face_not_tracked"
    elif motion_speech is None or motion_speech < settings.lip_motion_min:
        result["reason"], result["reason_code"] = "la boca no se mueve al hablar", "lips_not_moving"
    elif corr < settings.lip_corr_min:
        result["reason"], result["reason_code"] = "el movimiento de la boca no coincide con la voz", "lips_out_of_sync"
    else:
        result["passed"] = True
    return result

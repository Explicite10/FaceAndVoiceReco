"""Sincronia labios-voz: la boca debe moverse cuando suena la voz.

Toma la apertura de la boca cuadro por cuadro (face_track.py, MediaPipe) y
la correlaciona con la energia del audio.
"""
import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

from app.core.config import settings
from app.services.audio_extract import SAMPLE_RATE, AudioExtractionError, extract_audio_16k_mono
from app.services.face_track import TRACK_FPS

_ENVELOPE_WIN_SEC = 0.05
# La boca suele moverse un poco antes que el sonido; se busca el mejor desfase.
_MAX_LAG_SEC = 0.2


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


def analyze_lip_sync(video_path: str, track: dict) -> dict:
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

    times, ap = track["times"], track["aperture"]
    if len(times) == 0:
        result["reason"], result["reason_code"] = "no se pudo leer el video", "unreadable_video"
        return result

    tracked = ~np.isnan(ap)
    result["tracked_ratio"] = round(float(tracked.mean()), 3)
    speech = _speech_mask(audio, times)
    motion_speech = _mean_motion(ap, speech)
    motion_silence = _mean_motion(ap, ~speech)
    corr, lag = _best_lag_correlation(ap, _envelope(audio, times), int(round(_MAX_LAG_SEC * TRACK_FPS)))

    result.update(
        mouth_motion_speech=None if motion_speech is None else round(motion_speech, 5),
        mouth_motion_silence=None if motion_silence is None else round(motion_silence, 5),
        correlation=round(corr, 3) if corr > -1.0 else None,
        lag_ms=int(round(lag * 1000 / TRACK_FPS)) if corr > -1.0 else None,
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

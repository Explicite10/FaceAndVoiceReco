import asyncio
import logging
import time

import numpy as np
from faster_whisper.vad import VadOptions, get_speech_timestamps

from app.core.config import settings
from app.services.asr_engine import get_model, get_semaphore
from app.services.errors import CheckError
from app.services.audio_extract import (
    SAMPLE_RATE,
    AudioExtractionError,
    extract_audio_16k_mono,
)
from app.services.phrase_match import score_phrase

logger = logging.getLogger("uvicorn.error")


def _measure_speech(audio: np.ndarray) -> tuple[float, int]:
    vad_options = VadOptions(min_silence_duration_ms=300, speech_pad_ms=200)
    chunks = get_speech_timestamps(audio, vad_options, sampling_rate=SAMPLE_RATE)
    samples = sum(c["end"] - c["start"] for c in chunks)
    return samples / SAMPLE_RATE, len(chunks)


def _transcribe(audio: np.ndarray, language: str) -> dict:
    model = get_model()
    segments, info = model.transcribe(
        audio,
        language=language,
        beam_size=settings.asr_beam_size,
        temperature=0.0,
        condition_on_previous_text=False,
        vad_filter=True,
        word_timestamps=True,
        no_speech_threshold=settings.max_no_speech_prob,
    )

    # transcribe() devuelve un generador perezoso: el computo real ocurre aqui.
    segments = list(segments)

    text = " ".join(s.text.strip() for s in segments).strip()
    probs = [w.probability for s in segments for w in (s.words or [])]

    if segments:
        avg_logprob = float(np.mean([s.avg_logprob for s in segments]))
        no_speech_prob = float(max(s.no_speech_prob for s in segments))
    else:
        avg_logprob = -10.0
        no_speech_prob = 1.0

    return {
        "text": text,
        "avg_logprob": round(avg_logprob, 4),
        "no_speech_prob": round(no_speech_prob, 4),
        "mean_word_confidence": round(float(np.mean(probs)), 4) if probs else 0.0,
        "language": info.language,
        "language_probability": round(float(info.language_probability), 4),
        "segments": len(segments),
    }


def _empty_score(expected: str) -> dict:
    from app.services.text_norm import normalize_phrase

    expected_norm = normalize_phrase(expected)
    return {
        "match_percent": 0.0,
        "wer": 1.0,
        "wer_score": 0.0,
        "fuzzy_ratio": 0.0,
        "expected_normalized": expected_norm,
        "heard_normalized": "",
        "missing_words": expected_norm.split(),
        "extra_words": [],
        "ignored_words": [],
    }


def _build_response(
    *,
    decision: str,
    expected: str,
    score: dict,
    audio_info: dict,
    asr: dict | None,
    signals: list[str],
    elapsed_ms: int,
) -> dict:
    return {
        "decision": decision,
        "is_match": decision == "match",
        "match_percent": score["match_percent"],
        "threshold": settings.phrase_match_threshold,
        "expected_phrase": expected,
        "transcript": asr["text"] if asr else "",
        "scores": {
            "wer": score["wer"],
            "wer_score": score["wer_score"],
            "fuzzy_ratio": score["fuzzy_ratio"],
            "expected_normalized": score["expected_normalized"],
            "heard_normalized": score["heard_normalized"],
        },
        "missing_words": score["missing_words"],
        "extra_words": score["extra_words"],
        "ignored_words": score.get("ignored_words", []),
        "audio": audio_info,
        "asr": {
            "language": asr["language"] if asr else None,
            "language_probability": asr["language_probability"] if asr else None,
            "avg_logprob": asr["avg_logprob"] if asr else None,
            "no_speech_prob": asr["no_speech_prob"] if asr else None,
            "mean_word_confidence": asr["mean_word_confidence"] if asr else None,
            "segments": asr["segments"] if asr else 0,
        },
        "signals": signals,
        "model": {
            "size": settings.asr_model_size,
            "device": settings.asr_device,
            "compute_type": settings.asr_compute_type,
        },
        "elapsed_ms": elapsed_ms,
    }


# Margen alrededor del tramo de la frase: la persona puede empezar a hablar un
# poco antes de que aparezca en pantalla, y terminar despues de presionar el boton.
VENTANA_ANTES_SEC = 0.5
VENTANA_DESPUES_SEC = 1.0
VENTANA_MIN_SEC = 1.5


def recortar_ventana(audio: np.ndarray, ventana: tuple[float, float | None] | None) -> tuple[np.ndarray, dict | None]:
    """Deja solo el audio del tramo de la frase. Si el tramo no tiene sentido, devuelve todo."""
    if not ventana:
        return audio, None
    duracion = len(audio) / SAMPLE_RATE
    inicio, fin = ventana
    a = max(0.0, inicio - VENTANA_ANTES_SEC)
    b = min(duracion, (fin + VENTANA_DESPUES_SEC) if fin is not None else duracion)
    if b - a < VENTANA_MIN_SEC:
        return audio, {"usada": False, "motivo": "tramo_muy_corto"}
    return audio[int(a * SAMPLE_RATE):int(b * SAMPLE_RATE)], {"usada": True, "inicio_sec": round(a, 2), "fin_sec": round(b, 2)}


async def verify_voice_from_path(
    video_path: str,
    expected_phrase: str | None = None,
    ventana: tuple[float, float | None] | None = None,
) -> dict:
    """Transcribe y califica la frase. Con `ventana` (segundos del video donde se
    leyo) solo se escucha ese tramo: lo dicho antes, durante los gestos, no cuenta.
    """
    t0 = time.time()
    expected = (expected_phrase or settings.expected_phrase).strip()
    if not expected:
        raise CheckError(400, "empty_phrase", "La frase esperada esta vacia.")

    try:
        audio = await asyncio.to_thread(extract_audio_16k_mono, video_path)
    except AudioExtractionError as exc:
        raise CheckError(422, "audio_missing", str(exc)) from exc

    duration = len(audio) / SAMPLE_RATE
    audio_info = {
        "duration_sec": round(duration, 2),
        "speech_duration_sec": 0.0,
        "speech_ratio": 0.0,
        "speech_segments": 0,
        "sample_rate": SAMPLE_RATE,
    }

    # Gate barato: descarta por duracion antes de gastar CPU en el ASR.
    if duration < settings.audio_min_duration_sec:
        return _build_response(
            decision="invalid_audio",
            expected=expected,
            score=_empty_score(expected),
            audio_info=audio_info,
            asr=None,
            signals=[f"audio_muy_corto (<{settings.audio_min_duration_sec}s)"],
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    if duration > settings.audio_max_duration_sec:
        return _build_response(
            decision="invalid_audio",
            expected=expected,
            score=_empty_score(expected),
            audio_info=audio_info,
            asr=None,
            signals=[f"audio_muy_largo (>{settings.audio_max_duration_sec}s)"],
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    # Los limites de duracion se revisan con el video completo; la voz, solo en la frase.
    audio, ventana_info = recortar_ventana(audio, ventana)
    audio_info["ventana_frase"] = ventana_info
    duration = len(audio) / SAMPLE_RATE

    speech_sec, n_chunks = await asyncio.to_thread(_measure_speech, audio)
    speech_ratio = speech_sec / duration if duration else 0.0
    audio_info.update(
        speech_duration_sec=round(speech_sec, 2),
        speech_ratio=round(speech_ratio, 4),
        speech_segments=n_chunks,
    )

    # Sin voz suficiente Whisper alucina texto plausible: cortar aqui.
    if (
        speech_sec < settings.min_speech_duration_sec
        or speech_ratio < settings.min_speech_ratio
    ):
        return _build_response(
            decision="no_speech",
            expected=expected,
            score=_empty_score(expected),
            audio_info=audio_info,
            asr=None,
            signals=[f"voz_insuficiente ({speech_sec:.2f}s)"],
            elapsed_ms=int((time.time() - t0) * 1000),
        )

    async with get_semaphore():
        asr = await asyncio.to_thread(_transcribe, audio, settings.asr_language)

    signals: list[str] = []
    if asr["no_speech_prob"] > settings.max_no_speech_prob:
        signals.append(f"no_speech_prob_alto ({asr['no_speech_prob']})")
    if asr["avg_logprob"] < settings.min_avg_logprob:
        signals.append(f"avg_logprob_bajo ({asr['avg_logprob']})")

    score = score_phrase(expected, asr["text"])

    if signals:
        decision = "low_confidence"
    elif score["match_percent"] >= settings.phrase_match_threshold:
        decision = "match"
    else:
        decision = "no_match"

    return _build_response(
        decision=decision,
        expected=expected,
        score=score,
        audio_info=audio_info,
        asr=asr,
        signals=signals,
        elapsed_ms=int((time.time() - t0) * 1000),
    )

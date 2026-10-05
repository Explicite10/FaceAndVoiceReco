import asyncio
import logging
import threading
import time

from faster_whisper import WhisperModel

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

_model: WhisperModel | None = None
_load_lock = threading.Lock()
_semaphore: asyncio.Semaphore | None = None


def load_model() -> WhisperModel:
    """Carga el modelo una sola vez. Se llama desde el lifespan de FastAPI."""
    global _model

    if _model is not None:
        return _model

    with _load_lock:
        if _model is None:
            t0 = time.time()
            logger.info(
                "ASR: cargando modelo=%s device=%s compute=%s",
                settings.asr_model_size,
                settings.asr_device,
                settings.asr_compute_type,
            )
            _model = WhisperModel(
                settings.asr_model_size,
                device=settings.asr_device,
                compute_type=settings.asr_compute_type,
                cpu_threads=settings.asr_cpu_threads,
            )
            logger.info("ASR: modelo listo en %.2fs", time.time() - t0)

    return _model


def get_model() -> WhisperModel:
    if _model is None:
        return load_model()
    return _model


def get_semaphore() -> asyncio.Semaphore:
    """Limita transcripciones concurrentes: CTranslate2 ya usa varios hilos,
    y dos transcripciones a la vez en pocos cores se estorban entre si."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(settings.asr_max_concurrency)
    return _semaphore

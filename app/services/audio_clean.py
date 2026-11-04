"""Limpieza y medicion del audio antes de escuchar la frase.

Solo numpy: un pasa-altos por FFT quita el retumbo grave (ventilador, trafico,
manejo del telefono) y la normalizacion sube a quien habla bajo o lejos del
microfono. No es un supresor de voces de fondo: para eso lo que mas pesa es el
modelo de Whisper (medium o large en GPU) y el `initial_prompt` con la frase.
"""
import numpy as np

from app.services.audio_extract import SAMPLE_RATE

CORTE_GRAVES_HZ = 80.0
CORTE_AGUDOS_HZ = 7500.0
# Nivel objetivo (RMS) y techo tras normalizar: -20 dBFS ~ 0.1, -1 dBFS ~ 0.89.
RMS_OBJETIVO = 0.1
PICO_MAXIMO = 0.89
GANANCIA_MAXIMA = 20.0


def limpiar(audio: np.ndarray) -> np.ndarray:
    """Pasa-altos a 80 Hz, pasa-bajos a 7.5 kHz y normalizacion de volumen."""
    if len(audio) < SAMPLE_RATE // 10:
        return audio
    x = audio.astype(np.float32) - np.float32(audio.mean())
    espectro = np.fft.rfft(x)
    frec = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    # Rampas suaves (una octava) en vez de un corte seco: evita el "ringing".
    graves = np.clip((frec - CORTE_GRAVES_HZ / 2) / (CORTE_GRAVES_HZ / 2), 0.0, 1.0)
    agudos = np.clip((SAMPLE_RATE / 2 - frec) / (SAMPLE_RATE / 2 - CORTE_AGUDOS_HZ), 0.0, 1.0)
    x = np.fft.irfft(espectro * graves * agudos, n=len(x)).astype(np.float32)

    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 1e-6:
        return x
    ganancia = min(RMS_OBJETIVO / rms, GANANCIA_MAXIMA)
    pico = float(np.max(np.abs(x))) * ganancia
    if pico > PICO_MAXIMO:
        ganancia *= PICO_MAXIMO / pico
    return (x * ganancia).astype(np.float32)


def _rms_db(x: np.ndarray) -> float | None:
    if len(x) == 0:
        return None
    rms = float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))
    return round(20.0 * np.log10(max(rms, 1e-6)), 1)


def medir_ruido(audio: np.ndarray, tramos_voz: list[dict]) -> dict:
    """Nivel de la voz, del fondo (donde el VAD no oyo voz) y su diferencia en dB.

    `tramos_voz` son los chunks del VAD (start/end en muestras). Por debajo de
    ~10 dB de diferencia la transcripcion suele sufrir; por encima de 20 va bien.
    """
    mascara = np.zeros(len(audio), dtype=bool)
    for c in tramos_voz:
        mascara[int(c["start"]):int(c["end"])] = True
    voz, fondo = audio[mascara], audio[~mascara]
    voz_db, fondo_db = _rms_db(voz), _rms_db(fondo)
    return {
        "voz_db": voz_db,
        "fondo_db": fondo_db,
        "relacion_db": round(voz_db - fondo_db, 1) if voz_db is not None and fondo_db is not None else None,
        "pico": round(float(np.max(np.abs(audio))), 3) if len(audio) else None,
    }

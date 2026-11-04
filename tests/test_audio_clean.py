"""Limpieza y medicion del audio: solo numeros, no cambia la decision por si sola."""
import numpy as np

from app.services.audio_clean import limpiar, medir_ruido
from app.services.audio_extract import SAMPLE_RATE


def _tono(hz: float, seg: float, amp: float) -> np.ndarray:
    t = np.arange(int(SAMPLE_RATE * seg)) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_quita_graves_y_normaliza():
    voz, retumbo = _tono(300, 3, 0.02), _tono(30, 3, 0.3)
    limpio = limpiar(voz + retumbo)
    # El retumbo de 30 Hz se va y la voz baja sube a un nivel audible sin recortarse.
    espectro = np.abs(np.fft.rfft(limpio))
    frec = np.fft.rfftfreq(len(limpio), 1 / SAMPLE_RATE)
    assert espectro[np.argmin(np.abs(frec - 30))] < espectro[np.argmin(np.abs(frec - 300))] / 50
    assert 0.05 < float(np.sqrt(np.mean(limpio ** 2))) <= 0.11
    assert float(np.max(np.abs(limpio))) <= 0.9


def test_silencio_y_audio_corto_no_rompen():
    assert float(np.max(np.abs(limpiar(np.zeros(SAMPLE_RATE * 2, dtype=np.float32))))) == 0.0
    corto = np.ones(100, dtype=np.float32)
    assert limpiar(corto) is corto


def test_medir_ruido():
    voz, fondo = _tono(300, 1, 0.3), _tono(300, 1, 0.03)
    audio = np.concatenate([fondo, voz, fondo])
    m = medir_ruido(audio, [{"start": SAMPLE_RATE, "end": 2 * SAMPLE_RATE}])
    assert 19 <= m["relacion_db"] <= 21 and m["pico"] == 0.3
    assert medir_ruido(audio, [])["voz_db"] is None

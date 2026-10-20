"""Calificacion de la frase: tramo de la frase en el video y palabras de mas."""
import numpy as np

from app.services.audio_extract import SAMPLE_RATE
from app.services.audio_liveness import recortar_ventana
from app.services.identity_verification import ventana_valida
from app.services.phrase_match import score_phrase

FRASE = "Autorizo a la aseguradora a usar mis datos personales para contratar mi plan. Conozco el uso que se dará a mi información."


def _gestos(ultimo):
    return {"ok": True, "result": {"last_gesture_sec": ultimo}}


def test_palabras_antes_y_despues_no_cuentan():
    r = score_phrase(FRASE, "ya empezó? a ver. " + FRASE + " listo")
    assert r["match_percent"] == 100.0
    assert r["ignored_words"] == ["ya", "empezo", "a", "ver", "listo"]


def test_errores_dentro_de_la_frase_si_cuentan():
    r = score_phrase(FRASE, FRASE.replace("datos personales", "datos persona"))
    assert r["match_percent"] < 100 and r["missing_words"] == ["personales"] and r["ignored_words"] == []
    incompleta = score_phrase(FRASE, "Autorizo a la aseguradora a usar mis datos personales para contratar mi plan.")
    assert incompleta["match_percent"] < 85


def test_frase_revuelta_u_otra_no_pasa():
    revuelta = " ".join(reversed(FRASE.split()))
    assert score_phrase(FRASE, revuelta)["match_percent"] < 50
    assert score_phrase(FRASE, "hola buenos dias como estan todos espero que muy bien")["match_percent"] < 30


def test_ventana_solo_si_empieza_despues_de_los_gestos():
    assert ventana_valida((9.0, 18.0), _gestos(8.4)) == (9.0, 18.0)
    assert ventana_valida((8.0, 18.0), _gestos(8.4)) == (8.0, 18.0)  # dentro de la tolerancia
    assert ventana_valida((2.0, 18.0), _gestos(8.4)) is None          # antes de los gestos: se ignora
    assert ventana_valida((9.0, 18.0), _gestos(None)) is None         # gestos fuera de orden
    assert ventana_valida((9.0, 18.0), {"ok": False}) is None
    assert ventana_valida(None, _gestos(8.4)) is None
    assert ventana_valida((9.0, 5.0), _gestos(8.4)) == (9.0, None)    # fin incoherente: hasta el final


def test_recorte_del_audio():
    audio = np.zeros(SAMPLE_RATE * 20, dtype=np.float32)
    recortado, info = recortar_ventana(audio, (10.0, 15.0))
    assert info["usada"] and info["inicio_sec"] == 9.5 and info["fin_sec"] == 16.0
    assert len(recortado) == int(6.5 * SAMPLE_RATE)
    todo, info = recortar_ventana(audio, (19.9, None))  # tramo de menos de 1.5 s
    assert len(todo) == len(audio) and info == {"usada": False, "motivo": "tramo_muy_corto"}
    assert recortar_ventana(audio, None) == (audio, None)

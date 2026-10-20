"""Rostro con tramo de frase: la INE contra la frase, los gestos contra la frase."""
import numpy as np
import pytest

from app.services import face_service, report

STATS = {"multi_face_frames": 0, "max_faces_in_frame": 1, "first_multi_face_sec": None}


def _vec(angulo_grados: float) -> np.ndarray:
    """Vector unitario: el coseno entre dos es el coseno de la diferencia de angulos."""
    a = np.radians(angulo_grados)
    v = np.zeros(128, dtype=np.float32)
    v[0], v[1] = np.cos(a), np.sin(a)
    return v


@pytest.fixture(autouse=True)
def sin_modelos(monkeypatch):
    # El "recorte" de cada tramo ya es su vector: no hace falta SFace.
    monkeypatch.setattr(face_service, "embed", lambda crop, face: crop)


def _tramo(role, angulo, t):
    return {"crop": _vec(angulo), "face": None, "time_sec": t, "live_score": 0.9, "role": role, "frontal": role == "frase"}


def _comparar(frase, gesto1, gesto2):
    segs = [_tramo("gestos", gesto1, 2.0), _tramo("gestos", gesto2, 6.0), _tramo("frase", frase, 12.0)]
    return face_service.compare_id_against_segments(_vec(0), segs, STATS, 70)


def test_aprueba_aunque_un_gesto_este_de_lado():
    # Frase casi igual a la INE; un gesto "de perfil" queda lejos de la INE pero cerca de la frase.
    r = _comparar(frase=10, gesto1=20, gesto2=75)
    assert r["mode"] == "frase" and r["is_match"] and r["decision"] == "match"
    perfil = r["segments"][1]
    assert perfil["similarity_percent"] < 70 and perfil["same_person_percent"] >= 50
    checks = report._face_checks({"ok": True, "result": r})
    assert checks["face_match"]["status"] == "passed" and checks["same_person"]["status"] == "passed"


def test_otra_persona_en_los_gestos():
    r = _comparar(frase=10, gesto1=15, gesto2=150)
    assert not r["is_match"] and r["decision"] == "person_changed"
    checks = report._face_checks({"ok": True, "result": r})
    assert checks["same_person"]["status"] == "failed" and checks["same_person"]["code"] == "person_changed"
    assert checks["face_match"]["status"] == "passed"


def test_la_frase_no_coincide_con_la_ine():
    r = _comparar(frase=80, gesto1=80, gesto2=85)
    assert not r["is_match"] and r["decision"] == "no_match"
    checks = report._face_checks({"ok": True, "result": r})
    assert checks["face_match"]["code"] == "face_mismatch"


def test_sin_tramo_de_frase_sigue_igual_que_antes():
    segs = [_tramo(None, a, t) for a, t in ((10, 2.0), (75, 6.0), (10, 12.0))]
    r = face_service.compare_id_against_segments(_vec(0), segs, STATS, 70)
    assert "mode" not in r and not r["is_match"]  # un tramo de perfil todavia tumba el modo viejo
    assert "same_person" not in report._face_checks({"ok": True, "result": r})

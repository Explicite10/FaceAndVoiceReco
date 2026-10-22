"""Orientacion: un video grabado "acostado" se endereza antes de analizarlo.

El detector real necesita una cara; aqui se sustituye por uno que "ve una cara" solo
cuando una marca roja queda arriba a la izquierda (es decir, con el giro correcto).
"""
import cv2
import numpy as np
import pytest

from app.services import orientacion as ori


class _Cara:
    score = 0.9


@pytest.fixture(autouse=True)
def detector_falso(monkeypatch):
    def detect(rgb):
        return [_Cara()] if rgb[:40, :40, 0].mean() > 200 else []  # rojo arriba a la izquierda
    monkeypatch.setattr(ori, "detect_faces", detect)
    monkeypatch.setattr(ori, "pick_best_face", lambda caras: caras[0] if caras else None)
    ori._cache.clear()


def _video(path, esquina: str | None, cuadros=90, w=320, h=240):
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 15, (w, h))
    for _ in range(cuadros):
        f = np.zeros((h, w, 3), np.uint8)
        if esquina == "arriba-izq":
            f[:60, :60] = (0, 0, 255)
        elif esquina == "abajo-izq":   # como queda un retrato grabado acostado
            f[-60:, :60] = (0, 0, 255)
        out.write(f)
    out.release()
    return str(path)


def test_video_acostado_se_endereza(tmp_path):
    p = _video(tmp_path / "acostado.mp4", "abajo-izq")
    assert ori.orientacion(p) == 90                      # girar 90 grados horario la deja derecha
    f = np.zeros((240, 320, 3), np.uint8); f[-60:, :60] = (0, 0, 255)
    assert ori.girar(f, 90)[:40, :40, 2].mean() > 200    # la marca queda arriba a la izquierda


def test_video_derecho_no_se_toca(tmp_path):
    assert ori.orientacion(_video(tmp_path / "derecho.mp4", "arriba-izq")) == 0


def test_sin_caras_no_se_gira(tmp_path):
    assert ori.orientacion(_video(tmp_path / "vacio.mp4", None)) == 0


def test_archivo_inexistente(tmp_path):
    assert ori.orientacion(str(tmp_path / "no-existe.mp4")) == 0


def test_imagen_de_lado_se_endereza():
    # INE fotografiada con el celular en vertical: la marca (la "cara") quedo abajo a la izquierda.
    img = np.zeros((240, 320, 3), np.uint8); img[-60:, :60] = (255, 0, 0)  # RGB: rojo
    derecha, angulo = ori.enderezar_imagen(img)
    assert angulo == 90 and derecha[:40, :40, 0].mean() > 200


def test_imagen_derecha_no_se_toca():
    img = np.zeros((240, 320, 3), np.uint8); img[:60, :60] = (255, 0, 0)
    derecha, angulo = ori.enderezar_imagen(img)
    assert angulo == 0 and derecha is img

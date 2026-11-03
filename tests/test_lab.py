"""Laboratorio local: el mismo flujo de captura sin base ni webhook.

El analisis se simula; lo que se prueba es que la sesion local recorra la
captura real (reto, video, resultado) y no toque la base ni el webhook.
"""
import asyncio
import io
import json

import httpx
import numpy as np
import pytest
from fastapi import FastAPI
from PIL import Image

from app import lab
from app.services import entrega, sesiones

REPORTE = {
    "processing_time_ms": 1500,
    "result": {"approved": False, "failed_checks": ["face.face_match"], "summary": "Rechazado: la cara no coincide."},
    "checks": {"face": {"face_match": {"status": "failed", "code": "face_mismatch", "message": "No coincide."}}},
    "details": {"face": {"similarity": {"average_percent": 55.2}, "segments": []}},
    "errors": [],
}


@pytest.fixture
def lab_app(monkeypatch, tmp_path):
    monkeypatch.setattr(lab, "CARPETA", str(tmp_path))
    rgb = np.zeros((40, 40, 3), dtype=np.uint8)

    async def leer_ine(_upload):
        return rgb

    async def analizar(*_a, medidas=None, **_k):
        medidas["video"] = {"resolucion": "640 x 480", "tramos": [{"tramo": 1, "rol": "frase", "cara_px": 95}]}
        return REPORTE

    async def no_enviar(*_a, **_k):
        raise AssertionError("El laboratorio no debe mandar webhooks")

    async def embed(_rgb):
        return np.ones(128, dtype=np.float32)

    monkeypatch.setattr(sesiones, "_leer_ine", leer_ine)
    monkeypatch.setattr(sesiones, "analyze_video", analizar)
    monkeypatch.setattr(sesiones.evidencia, "guardar", lambda *a, **k: {})
    monkeypatch.setattr(entrega, "enviar", no_enviar)
    monkeypatch.setattr(lab, "embed_id_image", embed)
    monkeypatch.setattr(lab, "diagnostico_ine", lambda _rgb: {"imagen_px": "40 x 40", "caras_detectadas": 1,
                                                              "cara": None, "avisos": []})
    monkeypatch.setattr(lab.evidencia, "recorte_rostro", lambda _rgb: b"jpeg")
    monkeypatch.setattr(lab, "medir_cara", lambda _rgb: {"imagen_px": "40 x 40", "cara_px": 73})

    app = FastAPI()
    app.include_router(sesiones.captura_api)
    app.include_router(sesiones.paginas)
    app.include_router(lab.router)
    return app


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (40, 40)).save(buf, "PNG")
    return buf.getvalue()


async def test_flujo_completo(lab_app, tmp_path):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=lab_app), base_url="http://localhost") as c:
        assert (await c.get("/lab")).status_code == 200
        r = await c.post("/lab", files={"ine_imagen": ("ine.png", _png(), "image/png")},
                         data={"etiqueta": "Caso 1", "frase": "yo confirmo mi identidad hoy", "max_intentos": "1"})
        assert r.status_code == 303 and r.headers["location"].startswith("/lab/resultado/")
        s = sesiones._sesiones[r.headers["location"].rsplit("/", 1)[1]]
        token = s.token
        assert s.local and s.url_retorno is None
        # La computadora muestra el QR que lleva a la captura.
        assert "<svg" in (await c.get(f"/lab/resultado/{s.id}")).text

        assert (await c.get(f"/captura/{token}")).status_code == 200
        assert (await c.post(f"/api/v1/captura/{token}/reto")).status_code == 200
        r = await c.post(f"/api/v1/captura/{token}/video", files={"video": ("v.webm", b"\x1aE\xdf\xa3" * 64, "video/webm")})
        assert r.status_code == 202
        for _ in range(100):
            if s.estado == "completada":
                break
            await asyncio.sleep(0.02)
        assert s.estado == "completada" and s.aprobada is False
        assert s.historial[0]["respuesta"]["reporte"] == REPORTE
        # Las medidas van aparte: el reporte (lo que recibe el webhook) no cambia.
        medidas = s.historial[0]["respuesta"]["medidas"]
        assert medidas["ine"]["cara_px"] == 73 and medidas["video"]["tramos"][0]["cara_px"] == 95
        assert "medidas" not in REPORTE

        html = (await c.get(f"/lab/resultado/{s.id}")).text
        assert "Intento 1" in html and "Rechazado: la cara no coincide." in html and "face.face_match" in html
        assert (await c.get(f"/lab/resultado/{s.id}/rostro-ine.jpg")).content == b"jpeg"

    guardado = json.loads(next(tmp_path.iterdir()).read_text(encoding="utf-8"))
    assert guardado["etiqueta"] == "Caso 1" and guardado["intentos"][0]["aprobado"] is False
    sesiones._quitar_memoria(s)


async def test_solo_desde_localhost(lab_app):
    transporte = httpx.ASGITransport(app=lab_app, client=("192.168.1.20", 5000))
    async with httpx.AsyncClient(transport=transporte, base_url="http://x") as c:
        assert (await c.get("/lab")).status_code == 404

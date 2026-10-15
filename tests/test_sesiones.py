"""Flujo de sesiones de punta a punta, sin modelos: el analisis del video y la
cara de la INE se simulan, y el webhook del proyecto es un transporte falso.

Necesita PostgreSQL:
    docker run -d --name vida-pg-test -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:16.4-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres pytest -q
"""
import asyncio
import email.policy
import hashlib
import json
from email.parser import BytesParser
import os

import cv2
import httpx
import numpy as np
import pytest

pytestmark = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Falta TEST_DATABASE_URL")

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.services import entrega, evidencia, proyectos, sesiones  # noqa: E402
from app.services.errors import CheckError  # noqa: E402

INE = ("ine.jpg", b"cualquier-cosa", "application/octet-stream")
FRASE = "acepto los terminos del credito numero 4521"
WEBHOOK = "http://proyecto.test/webhooks/vida"


def _informe(aprobado: bool, codes: tuple[str, ...] = ()) -> dict:
    checks = {"challenge": {c: {"status": "failed", "code": c, "message": c} for c in codes}}
    return {
        "request_id": "r", "processing_time_ms": 5,
        "result": {"decision": "approved" if aprobado else "rejected", "approved": aprobado,
                   "failed_checks": [f"challenge.{c}" for c in codes], "summary": "s"},
        "checks": checks, "details": {}, "errors": [], "meta": {},
    }


def _partes_multipart(request: httpx.Request) -> dict[str, tuple[str, str, bytes]]:
    """{campo: (nombre, tipo, bytes)} de un cuerpo multipart/form-data."""
    crudo = b"Content-Type: " + request.headers["Content-Type"].encode() + b"\r\n\r\n" + request.read()
    mensaje = BytesParser(policy=email.policy.HTTP).parsebytes(crudo)
    return {
        p.get_param("name", header="content-disposition"): (p.get_filename(), p.get_content_type(), p.get_payload(decode=True))
        for p in mensaje.iter_parts()
    }


class Proyecto:
    """Backend falso: guarda lo que recibe y responde con `codigo`."""

    def __init__(self, secreto: str):
        self.secreto = secreto
        self.recibidos: list[dict] = []
        self.archivos: list[dict] = []
        self.codigo = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.headers["Content-Type"].startswith("multipart/form-data"):
            partes = _partes_multipart(request)
            cuerpo = partes.pop("evento")[2]  # se firma solo el JSON
        else:
            assert request.headers["Content-Type"] == "application/json"
            cuerpo, partes = request.read(), {}
        assert entrega.verificar(self.secreto, request.headers["X-Vida-Firma"], cuerpo)
        self.recibidos.append(json.loads(cuerpo))
        self.archivos.append(partes)
        return httpx.Response(self.codigo)


@pytest.fixture
async def entorno(monkeypatch):
    await db.connect()
    async with db.pool().acquire() as conn:
        await conn.execute("TRUNCATE intentos, sesiones, proyectos")
    sesiones._sesiones.clear()
    sesiones._por_token.clear()

    p = await proyectos.crear("Pruebas", WEBHOOK, ["proyecto.test"])
    otro = await proyectos.crear("Otro", WEBHOOK)
    backend = Proyecto(p["webhook_secreto"])
    monkeypatch.setattr(entrega, "transporte", httpx.MockTransport(backend))

    async def embed(np_id):
        if np_id is None:
            raise CheckError(422, "id_face_not_found", "sin cara")
        return np.ones(128, dtype=np.float32)

    async def leer_ine(ine):
        assert await ine.read() == INE[1]  # llega el binario tal cual
        return np.zeros((4, 4, 3))

    resultados: list[dict] = []

    async def analizar(*args, **kwargs):
        return resultados.pop(0)

    monkeypatch.setattr(sesiones, "embed_id_image", embed)
    monkeypatch.setattr(sesiones, "_leer_ine", leer_ine)
    monkeypatch.setattr(sesiones, "analyze_video", analizar)

    cliente = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://vida.test")
    yield {"cliente": cliente, "key": p["api_key"], "otro_key": otro["api_key"], "backend": backend,
           "resultados": resultados}
    await cliente.aclose()
    await db.close()


async def _crear(e, **extra):
    datos = {"frase": FRASE, "referencia_externa": "cliente-1", "url_retorno": "http://proyecto.test/fin", **extra}
    r = await e["cliente"].post("/api/v1/sesiones", headers={"X-API-Key": e["key"]}, files={"ine_imagen": INE},
                                data=datos)
    assert r.status_code == 201, r.text
    return r.json()


async def _intento(e, token: str) -> dict:
    c = e["cliente"]
    assert (await c.post(f"/api/v1/captura/{token}/reto")).status_code == 200
    r = await c.post(f"/api/v1/captura/{token}/video", files={"video": ("v.webm", b"x" * 10, "video/webm")})
    assert r.status_code == 202, r.text
    for _ in range(50):
        await asyncio.sleep(0.02)
        estado = (await c.get(f"/api/v1/captura/{token}")).json()
        if estado["estado"] not in ("procesando", "entregando"):
            return estado
    raise AssertionError("el intento no termino")


def _token(sesion: dict) -> str:
    return sesion["url_captura"].rsplit("/", 1)[1]


async def test_requiere_api_key_de_proyecto(entorno):
    r = await entorno["cliente"].post("/api/v1/sesiones", files={"ine_imagen": INE})
    assert r.status_code == 401


async def test_url_retorno_fuera_de_los_dominios(entorno):
    r = await entorno["cliente"].post("/api/v1/sesiones", headers={"X-API-Key": entorno["key"]},
                                      files={"ine_imagen": INE}, data={"frase": FRASE, "url_retorno": "http://otro.com/x"})
    assert r.status_code == 422


async def test_frase_obligatoria_y_de_largo_razonable(entorno):
    c, h = entorno["cliente"], {"X-API-Key": entorno["key"]}
    assert (await c.post("/api/v1/sesiones", headers=h, files={"ine_imagen": INE})).status_code == 422
    corta = await c.post("/api/v1/sesiones", headers=h, files={"ine_imagen": INE}, data={"frase": "hola"})
    assert corta.status_code == 422 and "palabras" in corta.text


async def test_metadata_debe_ser_objeto_json(entorno):
    r = await entorno["cliente"].post("/api/v1/sesiones", headers={"X-API-Key": entorno["key"]},
                                      files={"ine_imagen": INE}, data={"frase": FRASE, "metadata": "[1, 2]"})
    assert r.status_code == 422


async def test_aprobada_al_primer_intento(entorno):
    s = await _crear(entorno, metadata=json.dumps({"tramite": "alta"}))
    assert s["frase"] == FRASE
    reto = (await entorno["cliente"].post(f"/api/v1/captura/{_token(s)}/reto")).json()
    assert reto["phrase"] == FRASE and len(reto["gestures"]) == 3
    entorno["resultados"].append(_informe(True))
    final = await _intento(entorno, _token(s))

    assert final["estado"] == "completada" and final["aprobada"] is True
    assert final["redireccion"].startswith("http://proyecto.test/fin?sesion_id=")
    evento = entorno["backend"].recibidos[-1]
    assert evento["evento"] == "sesion.completada"
    assert evento["datos"]["sesion"]["referencia_externa"] == "cliente-1"
    assert evento["datos"]["sesion"]["metadata"] == {"tramite": "alta"}
    assert evento["datos"]["resultado"]["result"]["approved"] is True
    assert evento["datos"]["resumen"]["aprobada"] is True
    assert len(evento["datos"]["intentos"]) == 1

    row = await db.pool().fetchrow("SELECT * FROM sesiones WHERE id = $1", s["id"])
    assert row["estado"] == "completada" and row["aprobada"] is True
    assert sesiones._sesiones[s["id"]].id_emb is None  # la INE ya se olvido


async def test_reintento_y_luego_agota_intentos(entorno):
    s = await _crear(entorno, max_intentos="2")
    token = _token(s)
    entorno["resultados"].append(_informe(False, ("blink_not_detected",)))
    primero = await _intento(entorno, token)
    assert primero["estado"] == "pendiente"
    assert primero["intentos_restantes"] == 1
    assert any("parpadeo" in m for m in primero["motivos"])
    assert not entorno["backend"].recibidos  # aun no se entrega nada

    entorno["resultados"].append(_informe(False, ("spoof_detected",)))
    final = await _intento(entorno, token)
    assert final["estado"] == "completada" and final["aprobada"] is False
    assert any("foto o una pantalla" in m for m in final["motivos"])  # la pantalla final dice por que
    evento = entorno["backend"].recibidos[-1]
    assert [i["aprobado"] for i in evento["datos"]["intentos"]] == [False, False]


async def test_evidencia_del_intento_final(entorno):
    s = await _crear(entorno, incluir_video="true", incluir_fotogramas="true")
    token = _token(s)
    entorno["resultados"].append(_informe(False, ("blink_not_detected",)))
    await _intento(entorno, token)
    assert sesiones._sesiones[s["id"]].evidencia is None  # un intento fallido no se guarda
    entorno["resultados"].append(_informe(True))
    await _intento(entorno, token)

    ev = entorno["backend"].recibidos[-1]["datos"]["evidencia"]
    assert ev["intento"] == 2
    # El video falso no se puede decodificar: no hay fotogramas, pero si el video, en binario.
    assert ev["archivos"] == [{"campo": "video", "nombre": "video.webm", "tipo": "video/webm", "bytes": 10,
                               "sha256": hashlib.sha256(b"x" * 10).hexdigest()}]
    assert entorno["backend"].archivos[-1] == {"video": ("video.webm", "video/webm", b"x" * 10)}
    assert sesiones._sesiones[s["id"]].evidencia is None  # entregada: se olvida


async def test_sin_evidencia_por_defecto(entorno):
    s = await _crear(entorno)
    entorno["resultados"].append(_informe(True))
    await _intento(entorno, _token(s))
    assert entorno["backend"].recibidos[-1]["datos"]["evidencia"] is None
    assert entorno["backend"].archivos[-1] == {}  # JSON simple, como siempre


def test_fotogramas_de_un_video_real(tmp_path):
    path = str(tmp_path / "v.avi")
    out = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 15, (64, 48))
    for i in range(30):
        out.write(np.full((48, 64, 3), i * 8, dtype=np.uint8))
    out.release()
    fotos = evidencia.extraer_fotogramas(path)
    assert [f["campo"] for f in fotos] == [f"fotograma_{i}" for i in range(1, evidencia.FOTOGRAMAS + 1)]
    assert all(f["tipo"] == "image/jpeg" and f["nombre"].endswith(".jpg") for f in fotos)
    assert [f["segundo"] for f in fotos] == sorted(f["segundo"] for f in fotos)
    assert cv2.imdecode(np.frombuffer(fotos[0]["datos"], np.uint8), cv2.IMREAD_COLOR).shape == (48, 64, 3)
    descrito = evidencia.describir(1, fotos)["archivos"][0]
    assert "datos" not in descrito and descrito["sha256"] == hashlib.sha256(fotos[0]["datos"]).hexdigest()


async def test_video_sin_reto(entorno):
    s = await _crear(entorno)
    r = await entorno["cliente"].post(f"/api/v1/captura/{_token(s)}/video",
                                      files={"video": ("v.webm", b"x", "video/webm")})
    assert r.status_code == 409


async def test_error_de_entrega_y_reintento(entorno):
    s = await _crear(entorno)
    token = _token(s)
    entorno["backend"].codigo = 500
    entorno["resultados"].append(_informe(True))
    assert (await _intento(entorno, token))["estado"] == "error_entrega"

    entorno["backend"].codigo = 200
    r = await entorno["cliente"].post(f"/api/v1/captura/{token}/reintentar-entrega")
    assert r.json()["estado"] == "completada"
    assert len(entorno["backend"].recibidos) == 2


async def test_cancelar_avisa_al_proyecto(entorno):
    s = await _crear(entorno)
    r = await entorno["cliente"].post(f"/api/v1/sesiones/{s['id']}/cancelar", headers={"X-API-Key": entorno["key"]})
    assert r.json()["estado"] == "cancelada"
    await asyncio.sleep(0.05)
    assert entorno["backend"].recibidos[-1]["evento"] == "sesion.cancelada"
    assert (await entorno["cliente"].post(f"/api/v1/captura/{_token(s)}/reto")).status_code == 409


async def test_otro_proyecto_no_ve_la_sesion(entorno):
    s = await _crear(entorno)
    r = await entorno["cliente"].get(f"/api/v1/sesiones/{s['id']}", headers={"X-API-Key": entorno["otro_key"]})
    assert r.status_code == 404
    mia = await entorno["cliente"].get(f"/api/v1/sesiones/{s['id']}", headers={"X-API-Key": entorno["key"]})
    assert mia.status_code == 200


async def test_pagina_solo_se_incrusta_en_dominios_del_proyecto(entorno):
    s = await _crear(entorno)
    r = await entorno["cliente"].get(f"/captura/{_token(s)}")
    assert r.status_code == 200
    assert "frame-ancestors 'self' https://proyecto.test https://*.proyecto.test" in r.headers["content-security-policy"]


def test_resumen_con_reporte_completo():
    from app.services.report import build_summary

    ok = {"status": "passed", "code": None, "message": ""}
    bad = {"status": "failed", "code": "x", "message": ""}
    reporte = {
        "result": {"approved": False, "failed_checks": ["challenge.gesture_order"]},
        "checks": {
            "face": {"face_match": ok, "single_face": ok},
            "voice": {"phrase_match": ok},
            "liveness": {"anti_spoofing": ok, "lip_sync": ok},
            "challenge": {"head_turn": ok, "blink": ok, "hand_raise": ok, "gesture_order": bad},
        },
        "details": {
            "face": {"similarity": {"average_percent": 88.4, "threshold_percent": 70.0},
                     "segments": [{"face_detected": True, "similarity_percent": p} for p in (91.0, 86.2, 88.0)]},
            "voice": {"expected_phrase": FRASE, "transcript": "acepto los términos del crédito número 4521",
                      "phrase_match": {"match_percent": 96.5, "threshold_percent": 85.0}},
            "liveness": {"anti_spoofing": {"real_probability": 0.9312, "threshold_probability": 0.5}},
            "challenge": {"gestures": [{"gesture": "blink", "detected": True, "time_sec": 1.2}]},
        },
    }
    r = build_summary(reporte)
    assert r["rostro"] == {"coincide": True, "similitud_porcentaje": 88.4, "similitud_minima_porcentaje": 86.2,
                           "umbral_porcentaje": 70.0, "una_sola_persona": True}
    assert r["frase"]["coincidencia_porcentaje"] == 96.5 and r["frase"]["coincide"] is True
    assert r["vida"] == {"persona_real": True, "probabilidad_real_porcentaje": 93.1, "umbral_porcentaje": 50.0,
                         "labios_sincronizados": True}
    assert r["gestos"]["completos"] is True and r["gestos"]["en_orden"] is False
    assert r["aprobada"] is False


def _png(lado: int = 8) -> bytes:
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", (lado, lado), (20, 80, 200)).save(buf, "PNG")
    return buf.getvalue()


async def test_marca_colores_y_logo(entorno):
    c, h = entorno["cliente"], {"X-API-Key": entorno["key"]}
    r = await c.post("/api/v1/sesiones", headers=h,
                     files={"ine_imagen": INE, "logo": ("logo.png", _png(), "application/octet-stream")},
                     data={"frase": FRASE, "color_primario": "#18D", "color_secundario": "f5c400"})
    assert r.status_code == 201, r.text
    token = _token(r.json())
    pagina = (await c.get(f"/captura/{token}")).text
    assert "--accent: #1188dd; --accent-text: #ffffff;" in pagina
    assert "--accent-2: #f5c400;" in pagina
    assert f'src="/api/v1/captura/{token}/logo"' in pagina
    logo = await c.get(f"/api/v1/captura/{token}/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png"
    assert logo.headers["x-content-type-options"] == "nosniff"


async def test_sin_marca_usa_colores_genericos(entorno):
    s = await _crear(entorno)
    pagina = (await entorno["cliente"].get(f"/captura/{_token(s)}")).text
    assert "--accent:" not in pagina and "<header class=\"brand\"" not in pagina
    assert (await entorno["cliente"].get(f"/api/v1/captura/{_token(s)}/logo")).status_code == 404


async def test_marca_invalida(entorno):
    c, h = entorno["cliente"], {"X-API-Key": entorno["key"]}
    color = await c.post("/api/v1/sesiones", headers=h, files={"ine_imagen": INE},
                         data={"frase": FRASE, "color_primario": "rojo; } body { display:none"})
    assert color.status_code == 422
    svg = await c.post("/api/v1/sesiones", headers=h,
                       files={"ine_imagen": INE, "logo": ("l.svg", b"<svg onload='x()'/>", "image/svg+xml")},
                       data={"frase": FRASE})
    assert svg.status_code == 422


def test_texto_legible_sobre_el_color_primario():
    from app.services.marca import texto_sobre

    assert texto_sobre("#ffffff") == "#111111"
    assert texto_sobre("#f5c400") == "#111111"
    assert texto_sobre("#1b56d6") == "#ffffff"
    assert texto_sobre("#000000") == "#ffffff"

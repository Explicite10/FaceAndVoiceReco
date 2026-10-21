"""Expediente: INE, video y foto guardados en disco (binario) con su ruta en la base."""
import asyncio
import hashlib
import io

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from app import db
from app.core import acceso
from app.core.config import settings
from app.main import app
from app.services import expediente

EMAIL, PASSWORD = "admin@ejemplo.test", "clave-de-prueba-123"
JPG = b"\xff\xd8\xff\xe0" + bytes(range(256)) * 40          # empieza como JPEG
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 500
VIDEO = bytes((i * 7) % 256 for i in range(300_000))       # binario cualquiera, ~300 KB


@pytest.fixture(autouse=True)
def entorno(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "evidencia_dir", str(tmp_path / "evidencia"))
    monkeypatch.setattr(settings, "admin_email", EMAIL)
    monkeypatch.setattr(settings, "admin_password_hash", acceso.hash_password(PASSWORD))
    for lista in (db._mem_sesiones, db._mem_ajustes):
        lista.clear()
    db._mem_intentos.clear()
    db._mem_evidencias.clear()
    from app.core import rate_limit
    rate_limit.login_ip_limiter._hits.clear()
    rate_limit.login_global_limiter._hits.clear()


def subir(nombre: str, datos: bytes) -> UploadFile:
    u = UploadFile(file=io.BytesIO(datos), filename=nombre)
    asyncio.run(u.read())  # como si la verificacion ya lo hubiera leido completo
    return u


def intento_con_expediente(tipo="video"):
    sid = asyncio.run(db.crear_sesion())
    iid = asyncio.run(db.registrar_prueba(sid, tipo, False, ["face_mismatch"], 1000, {"result": {"approved": False}}))
    archivos = {"ine": subir("ine.jpeg", JPG), "video": subir("grabacion.webm", VIDEO)} if tipo == "video" \
        else {"ine": subir("ine.png", PNG), "foto": subir("foto.jpg", JPG)}
    guardados = asyncio.run(expediente.guardar(sid, iid, archivos))
    asyncio.run(db.registrar_evidencias(iid, guardados))
    return sid, iid, guardados


def admin() -> TestClient:
    c = TestClient(app, follow_redirects=False)
    c.post("/login", data={"email": EMAIL, "password": PASSWORD})
    return c


def test_guarda_en_binario_igual_byte_por_byte():
    sid, iid, g = intento_con_expediente()
    por_tipo = {x["tipo"]: x for x in g}
    assert por_tipo["ine"]["ruta"] == f"{sid}/{iid}/ine.jpg" and por_tipo["ine"]["mime"] == "image/jpeg"
    assert por_tipo["video"]["ruta"] == f"{sid}/{iid}/video.webm" and por_tipo["video"]["mime"] == "video/webm"
    for x, datos in ((por_tipo["ine"], JPG), (por_tipo["video"], VIDEO)):
        en_disco = (expediente.base() / x["ruta"]).read_bytes()
        assert en_disco == datos                                   # el archivo tal cual, sin base64
        assert x["bytes"] == len(datos) and x["sha256"] == hashlib.sha256(datos).hexdigest()


def test_tipo_de_imagen_por_contenido_no_por_nombre():
    _, _, g = intento_con_expediente("foto")
    por_tipo = {x["tipo"]: x for x in g}
    assert por_tipo["ine"]["ruta"].endswith("ine.png") and por_tipo["ine"]["mime"] == "image/png"
    assert por_tipo["foto"]["ruta"].endswith("foto.jpg")


def test_id_de_sesion_invalido_no_arma_rutas():
    with pytest.raises(ValueError):
        asyncio.run(expediente.guardar("../../etc", 1, {"ine": subir("x.jpg", JPG)}))
    assert expediente.ruta_absoluta("../../../etc/passwd") is None


def test_el_registro_lista_el_expediente_sin_rutas():
    sid, iid, _ = intento_con_expediente()
    r = asyncio.run(db.detalle_sesion(sid))
    exp = r["intentos"][0]["expediente"]
    assert sorted(e["tipo"] for e in exp) == ["ine", "video"]
    assert all(set(e) == {"id", "tipo", "mime", "bytes"} for e in exp)    # la ruta en disco nunca sale


def test_servir_solo_al_admin_completo_y_por_rangos():
    sid, iid, _ = intento_con_expediente()
    video = next(e for e in asyncio.run(db.detalle_sesion(sid))["intentos"][0]["expediente"] if e["tipo"] == "video")
    url = f"/panel/evidencia/{video['id']}"

    assert TestClient(app).get(url).status_code == 401                  # sin login
    c = admin()
    r = c.get(url)
    assert r.status_code == 200 and r.content == VIDEO and r.headers["content-type"] == "video/webm"
    assert r.headers["accept-ranges"] == "bytes" and "no-store" in r.headers["cache-control"]

    r = c.get(url, headers={"Range": "bytes=1000-1999"})                 # para adelantar el video
    assert r.status_code == 206 and r.content == VIDEO[1000:2000]
    assert r.headers["content-range"] == f"bytes 1000-1999/{len(VIDEO)}"
    assert c.get(url, headers={"Range": "bytes=-100"}).content == VIDEO[-100:]
    assert c.get(url, headers={"Range": f"bytes={len(VIDEO)}-"}).status_code == 416

    r = c.get(url + "?descargar=true")
    assert r.headers["content-disposition"].startswith("attachment") and ".webm" in r.headers["content-disposition"]
    assert c.get("/panel/evidencia/999999").status_code == 404


def test_borrar_sesion_borra_rutas_y_archivos():
    sid, iid, _ = intento_con_expediente()
    carpeta = expediente.base() / sid
    assert carpeta.exists()
    assert asyncio.run(db.borrar_sesion(sid)) is True
    assert not carpeta.exists() and db._mem_evidencias == [] and asyncio.run(db.detalle_sesion(sid)) is None
    assert asyncio.run(db.borrar_sesion(sid)) is False

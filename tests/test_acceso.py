"""Acceso a la demo: login del panel, entrada por QR y protecciones de la API.

Usa el TestClient sin lifespan (no carga modelos) y la base de datos en memoria.
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from app import db
from app.core import acceso, rate_limit
from app.core.config import settings
from app.main import app

EMAIL = "admin@ejemplo.test"
PASSWORD = "clave-de-prueba-123"


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", EMAIL)
    monkeypatch.setattr(settings, "admin_password_hash", acceso.hash_password(PASSWORD))
    db._mem_ajustes.clear()
    db._mem_sesiones.clear()
    acceso.set_qr_vigente(asyncio.run(db.qr_token()))
    for lim in (rate_limit.login_ip_limiter, rate_limit.login_global_limiter, rate_limit.qr_scan_limiter,
                rate_limit.challenge_limiter, rate_limit.verification_limiter):
        lim._hits.clear()


def cliente() -> TestClient:
    return TestClient(app, follow_redirects=False)


def login(c: TestClient, password: str = PASSWORD):
    return c.post("/login", data={"email": EMAIL, "password": password})


# ---------------------------------------------------------------- firma y contraseña

def test_cookie_firmada_no_se_puede_alterar():
    valor = acceso.firmar({"t": "a", "exp": 9_999_999_999})
    assert acceso.leer(valor)["t"] == "a"
    cuerpo, firma = valor.rsplit(".", 1)
    assert acceso.leer(cuerpo + "." + firma[:-2] + "xx") is None
    assert acceso.leer(cuerpo + ".ñ") is None  # no ASCII: no truena
    assert acceso.leer(acceso.firmar({"t": "a", "exp": 1})) is None  # vencida


def test_password_hash():
    h = acceso.hash_password("secreta")
    assert acceso.verificar_password("secreta", h)
    assert not acceso.verificar_password("otra", h)
    assert not acceso.verificar_password("secreta", "basura")


# ---------------------------------------------------------------- login y panel

def test_inicio_manda_al_login_y_el_panel_lo_pide():
    c = cliente()
    assert c.get("/").headers["location"] == "/login"
    assert c.get("/panel").headers["location"] == "/login"
    assert c.get("/panel/datos").status_code == 401


def test_login_incorrecto_y_correcto():
    c = cliente()
    assert login(c, "mala").status_code == 401
    assert c.post("/login", data={"email": "otro@x.test", "password": PASSWORD}).status_code == 401
    r = login(c)
    assert r.status_code == 303 and r.headers["location"] == "/panel"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=strict" in r.headers["set-cookie"].lower()
    panel = c.get("/panel")
    assert panel.status_code == 200 and "<svg" in panel.text and "/p/" in panel.text


def test_login_tiene_limite_por_ip():
    c = cliente()
    for _ in range(settings.login_attempts_per_ip):
        assert login(c, "mala").status_code == 401
    assert login(c).status_code == 429  # ni con la correcta mientras dure el bloqueo


def test_logout():
    c = cliente()
    login(c)
    c.post("/logout")
    assert c.get("/panel").headers["location"] == "/login"


# ---------------------------------------------------------------- QR y prueba

def test_prueba_y_api_cerradas_sin_qr():
    c = cliente()
    assert c.get("/prueba").status_code == 403
    assert c.post("/api/challenge").status_code == 401
    assert c.get("/p/token-que-no-existe").status_code == 404


def test_entrar_con_qr_abre_la_prueba_y_cuenta_la_sesion():
    c = cliente()
    token = asyncio.run(db.qr_token())
    r = c.get(f"/p/{token}")
    assert r.status_code == 303 and r.headers["location"] == "/prueba"
    assert c.get("/prueba").status_code == 200
    assert c.post("/api/challenge").status_code == 200
    # Volver a escanear con la misma cookie no crea otra sesion.
    c.get(f"/p/{token}")
    assert asyncio.run(db.resumen())["sesiones"] == 1


def test_qr_nuevo_invalida_el_anterior():
    admin, persona = cliente(), cliente()
    login(admin)
    viejo = asyncio.run(db.qr_token())
    persona.get(f"/p/{viejo}")
    admin.post("/panel/nuevo-qr")
    assert persona.post("/api/challenge").status_code == 401
    assert persona.get(f"/p/{viejo}").status_code == 404
    nuevo = asyncio.run(db.qr_token())
    assert nuevo != viejo and persona.get(f"/p/{nuevo}").status_code == 303


def test_registrar_prueba_en_la_sesion():
    sid = asyncio.run(db.crear_sesion())
    asyncio.run(db.registrar_prueba(sid, "video", True))
    asyncio.run(db.registrar_prueba(sid, "foto", False))
    r = asyncio.run(db.resumen())
    assert (r["sesiones"], r["personas"], r["pruebas"], r["aprobadas"], r["rechazadas"]) == (1, 1, 2, 0, 1)


# ---------------------------------------------------------------- protecciones generales

def test_request_demasiado_grande_se_corta():
    c = cliente()
    token = asyncio.run(db.qr_token())
    c.get(f"/p/{token}")
    grande = (settings.max_video_mb + settings.max_image_mb * 2 + 2) * 1024 * 1024
    r = c.post("/api/verify-photo", content=b"x", headers={"content-length": str(grande),
                                                            "content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413
    assert c.post("/login", content=b"a" * 20_000,
                  headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 413


def test_encabezados_y_sin_docs():
    c = cliente()
    r = c.get("/login")
    for h in ("content-security-policy", "x-frame-options", "x-content-type-options", "referrer-policy",
              "permissions-policy"):
        assert h in r.headers
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"
    for ruta in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(ruta).status_code == 404

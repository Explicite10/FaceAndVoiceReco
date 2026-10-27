"""Panel administrativo: login, cookie firmada y consultas sobre sesiones e intentos.

Las pruebas de acceso no necesitan base de datos. Las de las consultas
necesitan PostgreSQL (ver test_sesiones.py).
"""
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db
from app.core import acceso
from app.core.config import settings
from app.core.rate_limit import login_global_limiter, login_ip_limiter
from app.main import app

CORREO = "admin@ejemplo.com"
CLAVE = "una-clave-de-prueba"
con_base = pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="Falta TEST_DATABASE_URL")


@pytest.fixture
def cuenta(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", CORREO)
    monkeypatch.setattr(settings, "admin_password_hash", acceso.hash_password(CLAVE))
    # Cada prueba empieza sin intentos de login contados.
    login_ip_limiter._hits.clear()
    login_global_limiter._hits.clear()


def _cliente() -> httpx.AsyncClient:
    # Sin lifespan: no se cargan los modelos.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_password_hash():
    h = acceso.hash_password(CLAVE)
    assert h.startswith("scrypt$") and CLAVE not in h
    assert acceso.verificar_password(CLAVE, h)
    assert not acceso.verificar_password("otra", h)
    assert not acceso.verificar_password(CLAVE, "basura")


def test_cookie_alterada_o_vencida():
    valor = acceso.firmar({"t": "a", "exp": int(time.time()) + 60})
    assert acceso.leer(valor)["t"] == "a"
    cuerpo, firma = valor.rsplit(".", 1)
    assert acceso.leer(cuerpo + "." + firma[:-2] + "AA") is None
    assert acceso.leer(acceso.firmar({"t": "a", "exp": int(time.time()) - 1})) is None


def test_credenciales(cuenta):
    assert acceso.credenciales_validas(CORREO, CLAVE)
    assert acceso.credenciales_validas("  Admin@Ejemplo.com ", CLAVE)
    assert not acceso.credenciales_validas(CORREO, "otra")
    assert not acceso.credenciales_validas("otro@ejemplo.com", CLAVE)


def test_sin_cuenta_configurada_nadie_entra(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", "")
    monkeypatch.setattr(settings, "admin_password_hash", "")
    assert not acceso.credenciales_validas("", "")


async def test_panel_pide_login(cuenta):
    async with _cliente() as c:
        r = await c.get("/panel")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        for ruta in ("/panel/datos", "/panel/sesiones", "/panel/sesion/ses_x"):
            assert (await c.get(ruta)).status_code == 401
        r = await c.get("/login")
        assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]


async def test_login_y_logout(cuenta):
    async with _cliente() as c:
        r = await c.post("/login", data={"email": CORREO, "password": "mala"})
        assert r.status_code == 401 and acceso.COOKIE_ADMIN not in r.cookies
        r = await c.post("/login", data={"email": CORREO, "password": CLAVE})
        assert r.status_code == 303 and r.headers["location"] == "/panel"
        cookie = r.headers["set-cookie"].lower()
        assert "httponly" in cookie and "samesite=strict" in cookie
        assert (await c.get("/login")).headers["location"] == "/panel"
        r = await c.post("/logout")
        assert r.status_code == 303
        assert (await c.get("/panel")).headers["location"] == "/login"


async def test_login_limitado_por_ip(cuenta):
    async with _cliente() as c:
        for _ in range(settings.login_attempts_per_ip):
            assert (await c.post("/login", data={"email": CORREO, "password": "mala"})).status_code == 401
        # Ni con la clave correcta: la IP ya agoto sus intentos.
        assert (await c.post("/login", data={"email": CORREO, "password": CLAVE})).status_code == 429


async def test_raiz_lleva_al_panel():
    async with _cliente() as c:
        r = await c.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/panel"


# ---------------------------------------------------------------- con base de datos

@pytest.fixture
async def base(cuenta):
    await db.connect()
    async with db.pool().acquire() as conn:
        await conn.execute("TRUNCATE intentos, sesiones, proyectos")
    yield db.pool()
    await db.close()


async def _sesion(pool, sid, proyecto, estado, aprobada, intentos, hace_horas=1, entrega=None, ref=None):
    creada = datetime.now(timezone.utc) - timedelta(hours=hace_horas)
    await pool.execute(
        "INSERT INTO sesiones (id, proyecto_id, referencia_externa, estado, aprobada, intentos, max_intentos,"
        " webhook_url, entrega, creada, expira) VALUES ($1, $2, $3, $4, $5, $6, 3, 'https://x.test/w', $7, $8, $9)",
        sid, proyecto, ref, estado, aprobada, intentos, entrega, creada, creada + timedelta(minutes=30),
    )


async def _llenar(pool):
    for pid, nombre in (("prj_a", "Alfa"), ("prj_b", "Beta")):
        await pool.execute(
            "INSERT INTO proyectos (id, nombre, api_key_hash, webhook_secreto) VALUES ($1, $2, $1, 's')", pid, nombre)
    await _sesion(pool, "ses_1", "prj_a", "completada", True, 2, entrega={"ok": True, "codigo": 200}, ref="cliente-1")
    await _sesion(pool, "ses_2", "prj_a", "completada", False, 3, entrega={"ok": True, "codigo": 200})
    await _sesion(pool, "ses_3", "prj_b", "error_entrega", True, 1, entrega={"ok": False, "codigo": 500})
    await _sesion(pool, "ses_4", "prj_b", "expirada", None, 0)
    await _sesion(pool, "ses_5", "prj_b", "pendiente", None, 0)
    await _sesion(pool, "ses_viejo", "prj_a", "cancelada", None, 0, hace_horas=24 * 40)
    for sid, n, ok, checks in (
        ("ses_1", 1, False, ["challenge.blink"]), ("ses_1", 2, True, []),
        ("ses_2", 1, False, ["challenge.blink", "voice.phrase_match"]), ("ses_2", 2, False, ["challenge.blink"]),
        ("ses_2", 3, False, ["face.face_match"]), ("ses_3", 1, True, []),
    ):
        await pool.execute(
            "INSERT INTO intentos (sesion_id, numero, aprobado, checks_fallidos, duracion_ms) VALUES ($1, $2, $3, $4, 1000)",
            sid, n, ok, checks)


async def _entrar(c):
    assert (await c.post("/login", data={"email": CORREO, "password": CLAVE})).status_code == 303


@con_base
async def test_resumen(base):
    await _llenar(base)
    async with _cliente() as c:
        await _entrar(c)
        r = (await c.get("/panel/datos", params={"dias": 30})).json()
    assert r["sesiones"] == 5 and r["intentos"] == 6 and r["duracion_ms"] == 1000
    assert r["embudo"] == {"sesiones": 5, "con_intento": 3, "con_resultado": 3, "aprobadas": 2}
    assert r["resultado"] == {"aprobadas": 2, "no_aprobadas": 1, "en_curso": 1, "expiradas": 1, "canceladas": 0}
    assert r["entregas_fallidas"] == 1
    assert r["motivos"][0] == {"codigo": "challenge.blink", "n": 3}
    assert {p["nombre"] for p in r["por_proyecto"]} == {"Alfa", "Beta"}
    act = r["actividad"]
    assert act["unidad"] == "dia" and sum(p["sesiones"] for p in act["puntos"]) == 5
    assert sum(p["intentos"] for p in act["puntos"]) == 6

    async with _cliente() as c:
        await _entrar(c)
        todo = (await c.get("/panel/datos", params={"dias": 0})).json()
        alfa = (await c.get("/panel/datos", params={"dias": 0, "proyecto": "prj_a"})).json()
        hoy = (await c.get("/panel/datos", params={"dias": 1})).json()
    assert todo["sesiones"] == 6 and todo["resultado"]["canceladas"] == 1
    assert alfa["sesiones"] == 3 and alfa["intentos"] == 5
    assert hoy["actividad"]["unidad"] == "hora"


@con_base
async def test_tabla_y_detalle(base):
    await _llenar(base)
    async with _cliente() as c:
        await _entrar(c)
        p = (await c.get("/panel/sesiones", params={"dias": 0})).json()
        assert p["total"] == 6 and p["paginas"] == 1 and p["filas"][-1]["id"] == "ses_viejo"
        fallidas = (await c.get("/panel/sesiones", params={"resultado": "entrega_fallida"})).json()
        assert [f["id"] for f in fallidas["filas"]] == ["ses_3"]
        aprobadas = (await c.get("/panel/sesiones", params={"resultado": "aprobada"})).json()
        assert {f["id"] for f in aprobadas["filas"]} == {"ses_1", "ses_3"}
        busqueda = (await c.get("/panel/sesiones", params={"buscar": "cliente-1"})).json()
        assert [f["id"] for f in busqueda["filas"]] == ["ses_1"]
        # Los comodines de LIKE se buscan como texto, no traen todo.
        assert (await c.get("/panel/sesiones", params={"buscar": "%"})).json()["total"] == 0

        d = (await c.get("/panel/sesion/ses_2")).json()
        assert d["sesion"]["proyecto"] == "Alfa" and d["sesion"]["resultado"] == "no_aprobada"
        assert [i["numero"] for i in d["intentos"]] == [3, 2, 1]
        assert (await c.get("/panel/sesion/ses_no_existe")).status_code == 404

        html = (await c.get("/panel")).text
        assert "Alfa" in html and "apexcharts" in html

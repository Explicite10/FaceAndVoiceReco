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
    db._mem_intentos.clear()
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


def test_rafaga_de_robots_no_gasta_el_limite(monkeypatch):
    # Todos llegan con la misma IP (como detras del proxy). Limite bajo para la prueba.
    monkeypatch.setattr(rate_limit.qr_scan_limiter, "limit", 3)
    token = asyncio.run(db.qr_token())
    for _ in range(200):  # una extension de iPhone abriendo el enlace sin parar
        assert cliente().get(f"/p/{token}").status_code == 303
    personas = [cliente() for _ in range(3)]
    for c in personas:
        c.get(f"/p/{token}")
        assert c.post("/api/visita").status_code == 204       # el limite sigue completo
        for _ in range(5):                                       # recargar no gasta limite
            c.get(f"/p/{token}")
            assert c.post("/api/visita").status_code == 204
    assert asyncio.run(db.resumen())["sesiones"] == 3
    cuarta = cliente()
    cuarta.get(f"/p/{token}")
    assert cuarta.post("/api/visita").status_code == 429         # el limite aplica a registros nuevos
    assert asyncio.run(db.resumen())["sesiones"] == 3


def test_borrar_todo_no_deja_sin_contar_a_quien_ya_entro():
    c = cliente()
    token = asyncio.run(db.qr_token())
    c.get(f"/p/{token}")
    # Se borran todos los registros mientras la persona sigue con su cookie vigente.
    db._mem_sesiones.clear()
    db._mem_intentos.clear()
    assert c.post("/api/challenge").status_code == 200        # sigue pudiendo usar la prueba
    r = asyncio.run(db.resumen())
    assert r["sesiones"] == 1 and r["embudo"]["iniciaron"] == 1  # y se vuelve a contar
    db._mem_sesiones.clear()
    assert c.get(f"/p/{token}").status_code == 303            # re-escanear y cargar la pagina la recrea
    assert c.post("/api/visita").status_code == 204
    assert asyncio.run(db.resumen())["sesiones"] == 1
    sid = next(iter(db._mem_sesiones))
    db._mem_sesiones.clear()
    assert asyncio.run(db.registrar_prueba(sid, "video", True)) is not None  # y enviar una prueba
    assert asyncio.run(db.resumen())["personas"] == 1


def test_robot_que_solo_visita_el_enlace_no_cuenta():
    token = asyncio.run(db.qr_token())
    for _ in range(20):  # como un revisor de enlaces: sigue la redireccion, sin JavaScript
        robot = cliente()
        assert robot.get(f"/p/{token}").status_code == 303
        assert robot.get("/prueba").status_code == 200
    assert asyncio.run(db.resumen())["sesiones"] == 0
    persona = cliente()
    persona.get(f"/p/{token}")
    assert persona.post("/api/visita").status_code == 204      # la pagina cargo en un navegador
    assert persona.post("/api/visita").status_code == 204      # recargar no duplica
    assert asyncio.run(db.resumen())["sesiones"] == 1
    assert cliente().post("/api/visita").status_code == 401    # sin cookie del QR no se registra nada


def test_limite_de_escaneos_por_defecto_alto():
    assert settings.qr_scans_per_ip >= 300


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
    asyncio.run(db.registrar_prueba(sid, "foto", False, ["face_mismatch"]))
    r = asyncio.run(db.resumen())
    assert (r["sesiones"], r["personas"], r["pruebas"], r["aprobadas"], r["rechazadas"]) == (1, 1, 2, 0, 1)


def test_embudo_motivos_y_actividad():
    solo_escaneo = asyncio.run(db.crear_sesion())
    inicio = asyncio.run(db.crear_sesion())
    asyncio.run(db.registrar_reto(inicio))
    aprobada = asyncio.run(db.crear_sesion())
    asyncio.run(db.registrar_reto(aprobada))
    asyncio.run(db.registrar_prueba(aprobada, "video", True))
    rechazada = asyncio.run(db.crear_sesion())
    asyncio.run(db.registrar_reto(rechazada))
    asyncio.run(db.registrar_prueba(rechazada, "video", False, ["blink_not_detected", "phrase_mismatch"]))
    asyncio.run(db.registrar_prueba(rechazada, "video", False, ["blink_not_detected"]))

    r = asyncio.run(db.resumen())
    assert r["embudo"] == {"escanearon": 4, "iniciaron": 3, "verificaron": 2, "aprobadas": 1}
    assert r["resultado"] == {"aprobadas": 1, "no_aprobadas": 1, "abandonaron": 2}
    assert r["motivos"][0] == {"codigo": "blink_not_detected", "n": 2}
    assert sum(a["escaneos"] for a in r["actividad"]) == 4 and sum(a["pruebas"] for a in r["actividad"]) == 3
    # La tabla solo muestra las que terminaron una prueba; el resto vive en las graficas.
    etapas = sorted(s["etapa"] for s in asyncio.run(db.sesiones_pagina(1))["filas"])
    assert etapas == ["aprobada", "no_aprobada"]
    assert solo_escaneo  # (solo para nombrar la sesion que se quedo en el escaneo)


def test_pruebas_anteriores_a_intentos_siguen_contando():
    # Sesion de antes de la tabla intentos: tiene pruebas en el resumen pero sin filas de intentos.
    sid = asyncio.run(db.crear_sesion())
    db._mem_sesiones[sid].update(pruebas=3, aprobadas=1, ultima_prueba=db._ahora(), ultimo_tipo="video",
                                 ultimo_aprobado=True)
    r = asyncio.run(db.resumen())
    assert r["pruebas"] == 3 and sum(a["pruebas"] for a in r["actividad"]) == 3
    assert r["embudo"]["verificaron"] == 1 and r["motivos"] == [] and r["motivos_parciales"]


def test_ver_registro_de_una_sesion():
    sid = asyncio.run(db.crear_sesion())
    asyncio.run(db.registrar_reto(sid))
    detalle = {"result": {"approved": False}, "checks": {"voice": {"phrase_match": {"status": "failed", "code": "phrase_mismatch"}}},
               "details": {"voice": {"phrase_match": {"match_percent": 41.0}}}, "processing_time_ms": 8000}
    asyncio.run(db.registrar_prueba(sid, "video", False, ["phrase_mismatch"], 8000, detalle))
    asyncio.run(db.registrar_prueba(sid, "video", True, [], 7000, {**detalle, "result": {"approved": True}}))

    c = cliente()
    assert c.get(f"/panel/sesion/{sid}").status_code == 401  # solo el admin
    login(c)
    assert c.get("/panel/sesion/no-existe").status_code == 404
    r = c.get(f"/panel/sesion/{sid}").json()
    assert r["sesion"]["etapa"] == "aprobada" and r["sesion"]["retos"] == 1 and r["sin_detalle"] == 0
    assert [i["aprobado"] for i in r["intentos"]] == [True, False]  # el mas reciente primero
    assert r["intentos"][1]["detalle"]["details"]["voice"]["phrase_match"]["match_percent"] == 41.0
    # La tabla del panel trae el id para abrir el registro.
    assert c.get("/panel/sesiones").json()["filas"][0]["id"] == sid


def test_registro_con_pruebas_anteriores_al_detalle():
    sid = asyncio.run(db.crear_sesion())
    db._mem_sesiones[sid].update(pruebas=2, ultima_prueba=db._ahora(), ultimo_aprobado=False)
    r = asyncio.run(db.detalle_sesion(sid))
    assert r["intentos"] == [] and r["sin_detalle"] == 2


def test_tabla_paginada_de_10_en_10():
    from datetime import timedelta
    ids = []
    for n in range(23):
        sid = asyncio.run(db.crear_sesion())
        asyncio.run(db.registrar_prueba(sid, "video", n % 2 == 0))
        db._mem_sesiones[sid]["creada"] = db._ahora() + timedelta(seconds=n)  # la ultima es la mas reciente
        ids.append(sid)
    asyncio.run(db.crear_sesion())  # solo escaneo: no sale en la tabla

    c = cliente()
    assert c.get("/panel/sesiones").status_code == 401  # solo el admin
    login(c)
    p1 = c.get("/panel/sesiones").json()
    assert (p1["total"], p1["paginas"], p1["pagina"], len(p1["filas"])) == (23, 3, 1, 10)
    assert [f["id"] for f in p1["filas"]] == ids[::-1][:10]  # mas reciente primero
    p3 = c.get("/panel/sesiones?pagina=3").json()
    assert (p3["pagina"], len(p3["filas"])) == (3, 3) and p3["filas"][-1]["id"] == ids[0]
    assert c.get("/panel/sesiones?pagina=99").json()["pagina"] == 3  # fuera de rango: la ultima
    assert c.get("/panel/sesiones?pagina=0").status_code == 422
    assert "sesionesIniciales" in c.get("/panel").text


def test_tabla_sin_sesiones():
    p = asyncio.run(db.sesiones_pagina(5))
    assert (p["total"], p["paginas"], p["pagina"], p["filas"]) == (0, 1, 1, [])


def test_pedir_reto_cuenta_como_inicio():
    c = cliente()
    c.get(f"/p/{asyncio.run(db.qr_token())}")
    assert c.post("/api/challenge").status_code == 200
    assert asyncio.run(db.resumen())["embudo"]["iniciaron"] == 1


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

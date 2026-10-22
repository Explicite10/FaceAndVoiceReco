"""Paginas de la demo: login, panel con el QR, entrada por QR y la prueba de vida.

/            -> /panel si hay sesion de admin; si no, /login
/login       -> unica cuenta (ADMIN_EMAIL); limitado por IP y en total
/panel       -> QR con instrucciones, graficas y sesiones (solo admin)
/panel/sesion/<id> -> una sesion con el resultado de cada intento (solo admin)
/panel/evidencia/<id> -> un archivo del expediente: INE, video o foto (solo admin)
/p/<token>   -> lo que abre el QR: crea la sesion, pone la cookie y manda a /prueba
/prueba      -> la prueba de vida (solo con QR vigente o sesion de admin)
"""
import asyncio
import logging

import segno
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.core import acceso
from app.core.config import settings
from app.core.rate_limit import login_global_limiter, login_ip_limiter, qr_scan_limiter
from app.services import expediente

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(include_in_schema=False)
logger = logging.getLogger("uvicorn.error")


def _ir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _url_qr(request: Request, token: str) -> str:
    base = settings.public_url or str(request.base_url).rstrip("/")
    return f"{base}/p/{token}"


@router.get("/")
def inicio(request: Request):
    return _ir("/panel" if acceso.es_admin(request) else "/login")


# ---------------------------------------------------------------- login

@router.get("/login", response_class=HTMLResponse)
def login_pagina(request: Request):
    if acceso.es_admin(request):
        return _ir("/panel")
    return templates.TemplateResponse("login.html", {"request": request, "error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse,
             dependencies=[Depends(login_ip_limiter), Depends(login_global_limiter)])
async def login(request: Request, email: str = Form(..., max_length=200), password: str = Form(..., max_length=200)):
    if not acceso.credenciales_validas(email, password):
        # Un respiro en cada fallo hace mas lenta la fuerza bruta.
        await asyncio.sleep(0.8)
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "Correo o contraseña incorrectos.", "email": email[:200]},
            status_code=401,
        )
    resp = _ir("/panel")
    acceso.poner_cookie(resp, request, acceso.COOKIE_ADMIN, {"t": "a"},
                        settings.admin_session_hours * 3600, samesite="strict")
    return resp


@router.post("/logout")
def logout():
    resp = _ir("/login")
    resp.delete_cookie(acceso.COOKIE_ADMIN, path="/")
    return resp


# ---------------------------------------------------------------- panel

@router.get("/panel", response_class=HTMLResponse)
async def panel(request: Request):
    if not acceso.es_admin(request):
        return _ir("/login")
    url = _url_qr(request, await db.qr_token())
    # omitsize: solo viewBox, sin ancho/alto fijos; asi el CSS lo escala sin deformarlo.
    qr_svg = segno.make(url, error="m").svg_inline(omitsize=True, border=2, dark="#072146", light="#ffffff")
    return templates.TemplateResponse("panel.html", {
        "request": request,
        "qr_svg": qr_svg,
        "qr_url": url,
        "resumen": await db.resumen(),
        # Primera pagina de la tabla ya en el HTML: aparece sin esperar otra peticion.
        "sesiones": await db.sesiones_pagina(1),
        "persistente": db.enabled(),
    })


@router.get("/panel/evidencia/{evid_id}")
async def panel_evidencia(request: Request, evid_id: int, descargar: bool = False):
    """Un archivo del expediente (INE, video o foto). Solo admin; con rangos para el video."""
    if not acceso.es_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    e = await db.obtener_evidencia(evid_id)
    path = expediente.ruta_absoluta(e["ruta"]) if e else None
    if path is None:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    nombre = f'{e["tipo"]}-{e["creado"]:%Y%m%d-%H%M%S}{path.suffix}'
    return expediente.servir(request, path, e["mime"], nombre, descargar)


@router.get("/panel/sesiones")
async def panel_sesiones(request: Request, pagina: int = Query(1, ge=1, le=1_000_000)):
    """Una pagina de la tabla de sesiones (10 por pagina, la mas reciente primero)."""
    if not acceso.es_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return await db.sesiones_pagina(pagina)


@router.get("/panel/datos")
async def panel_datos(request: Request):
    if not acceso.es_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return await db.resumen()


@router.get("/panel/sesion/{sid}")
async def panel_sesion(request: Request, sid: str):
    """Una sesion con sus intentos y el resultado de cada uno (modal "Ver registro")."""
    if not acceso.es_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    datos = await db.detalle_sesion(sid) if len(sid) <= 64 else None
    if datos is None:
        return JSONResponse({"detail": "No encontrado"}, status_code=404)
    return datos


@router.post("/panel/nuevo-qr")
async def nuevo_qr(request: Request):
    if not acceso.es_admin(request):
        return _ir("/login")
    acceso.set_qr_vigente(await db.rotar_qr())
    return _ir("/panel")


# ---------------------------------------------------------------- QR y prueba

def _sin_acceso(request: Request, status: int, espera: int | None = None) -> HTMLResponse:
    resp = templates.TemplateResponse("acceso.html", {"request": request, "espera": espera}, status_code=status)
    if espera:
        resp.headers["Retry-After"] = str(espera)
    return resp


@router.get("/p/{token}")
async def entrar_con_qr(request: Request, token: str):
    # Si ya tenia sesion (volvio a escanear o recargo), se conserva y no cuenta para el limite.
    sid = acceso.sesion_participante(request)
    if not sid:
        try:
            qr_scan_limiter(request)
        except HTTPException as e:
            # Pagina para el celular, no un JSON: "mucha gente entrando, intenta en un momento".
            return _sin_acceso(request, 429, espera=int(e.headers.get("Retry-After", "30")))
    if len(token) > 100 or not acceso.token_qr_valido(token):
        return _sin_acceso(request, 404)
    # Que navegador abre el enlace: para distinguir personas de robots (revisores de
    # enlaces de correo/chat, escaneres) si el tablero vuelve a llenarse de golpe.
    agente = (request.headers.get("user-agent") or "-").replace("\n", " ")[:160]
    logger.info("QR abierto (%s): %s", "sesion existente" if sid else "nuevo", agente)
    # El enlace solo entrega la cookie. El registro en la base se crea cuando la pagina de
    # la prueba carga en un navegador real (POST /api/visita) o la persona pide un reto:
    # un robot que solo visita el enlace no infla el tablero.
    sid = sid or db.nuevo_id_sesion()
    resp = _ir("/prueba")
    # Lax y no Strict: el QR se abre desde la app de la camara (otro "sitio").
    acceso.poner_cookie(resp, request, acceso.COOKIE_ACCESO,
                        {"t": "p", "sid": sid, "q": acceso.huella_qr(token)},
                        settings.participant_session_minutes * 60, samesite="lax")
    return resp


@router.get("/prueba", response_class=HTMLResponse)
def prueba(request: Request):
    if not (acceso.sesion_participante(request) or acceso.es_admin(request)):
        return _sin_acceso(request, 403)
    return templates.TemplateResponse("index.html", {
        "request": request,
        "min_duration_sec": settings.audio_min_duration_sec,
        "max_duration_sec": settings.audio_max_duration_sec,
        "turn_offset": settings.gesture_turn_offset,
        "blink_closed": settings.gesture_blink_closed,
        "blink_open": settings.gesture_blink_open,
    })

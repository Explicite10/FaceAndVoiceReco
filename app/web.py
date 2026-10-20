"""Paginas de la demo: login, panel con el QR, entrada por QR y la prueba de vida.

/            -> /panel si hay sesion de admin; si no, /login
/login       -> unica cuenta (ADMIN_EMAIL); limitado por IP y en total
/panel       -> QR con instrucciones y conteo de sesiones (solo admin)
/p/<token>   -> lo que abre el QR: crea la sesion, pone la cookie y manda a /prueba
/prueba      -> la prueba de vida (solo con QR vigente o sesion de admin)
"""
import asyncio

import segno
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.core import acceso
from app.core.config import settings
from app.core.rate_limit import login_global_limiter, login_ip_limiter, qr_scan_limiter

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(include_in_schema=False)


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
    qr_svg = segno.make(url, error="m").svg_inline(scale=8, border=2, dark="#072146", light="#ffffff")
    return templates.TemplateResponse("panel.html", {
        "request": request,
        "qr_svg": qr_svg,
        "qr_url": url,
        "resumen": await db.resumen(),
        "persistente": db.enabled(),
    })


@router.get("/panel/datos")
async def panel_datos(request: Request):
    if not acceso.es_admin(request):
        return JSONResponse({"detail": "No autorizado"}, status_code=401)
    return await db.resumen()


@router.post("/panel/nuevo-qr")
async def nuevo_qr(request: Request):
    if not acceso.es_admin(request):
        return _ir("/login")
    acceso.set_qr_vigente(await db.rotar_qr())
    return _ir("/panel")


# ---------------------------------------------------------------- QR y prueba

def _sin_acceso(request: Request, status: int) -> HTMLResponse:
    return templates.TemplateResponse("acceso.html", {"request": request}, status_code=status)


@router.get("/p/{token}", dependencies=[Depends(qr_scan_limiter)])
async def entrar_con_qr(request: Request, token: str):
    if len(token) > 100 or not acceso.token_qr_valido(token):
        return _sin_acceso(request, 404)
    # Si ya tenia sesion con este QR (volvio a escanear), se conserva la misma.
    sid = acceso.sesion_participante(request) or await db.crear_sesion()
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

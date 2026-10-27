"""Panel administrativo: login y tablero con las sesiones de todos los proyectos.

/login                -> unica cuenta (ADMIN_EMAIL); limitado por IP y en total
/panel                -> totales, graficas y tabla de sesiones (solo admin)
/panel/datos          -> totales y graficas en JSON (refresco cada 15 s)
/panel/sesiones       -> una pagina de la tabla
/panel/sesion/<id>    -> una sesion con sus intentos (modal "Ver registro")

Solo lee el resumen que ya se guarda en PostgreSQL: no toca la API ni la captura.
"""
import asyncio

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db
from app.core import acceso
from app.core.config import settings
from app.core.rate_limit import login_global_limiter, login_ip_limiter
from app.services import panel

templates = Jinja2Templates(directory="app/templates")
router = APIRouter(include_in_schema=False)

# ApexCharts sale de jsDelivr y la fuente de Google Fonts; todo lo demas del propio sitio.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data:",
    "connect-src 'self'",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
])
_ENCABEZADOS = {
    "Content-Security-Policy": _CSP,
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _ir(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303, headers=_ENCABEZADOS)


def _pagina(request: Request, plantilla: str, datos: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(plantilla, {"request": request, **datos}, status_code=status,
                                      headers=_ENCABEZADOS)


def _no_autorizado() -> JSONResponse:
    return JSONResponse({"detail": "No autorizado"}, status_code=401, headers=_ENCABEZADOS)


def _sin_base() -> JSONResponse:
    return JSONResponse({"detail": "La base de datos no está configurada."}, status_code=503, headers=_ENCABEZADOS)


# ---------------------------------------------------------------- login

@router.get("/login", response_class=HTMLResponse)
def login_pagina(request: Request):
    if acceso.es_admin(request):
        return _ir("/panel")
    return _pagina(request, "login.html", {"error": None, "email": ""})


@router.post("/login", response_class=HTMLResponse,
             dependencies=[Depends(login_ip_limiter), Depends(login_global_limiter)])
async def login(request: Request, email: str = Form(..., max_length=200), password: str = Form(..., max_length=200)):
    if not acceso.credenciales_validas(email, password):
        # Un respiro en cada fallo hace mas lenta la fuerza bruta.
        await asyncio.sleep(0.8)
        return _pagina(request, "login.html", {"error": "Correo o contraseña incorrectos.", "email": email[:200]},
                       status=401)
    resp = _ir("/panel")
    acceso.abrir_sesion(resp, request)
    return resp


@router.post("/logout")
def logout():
    resp = _ir("/login")
    acceso.cerrar_sesion(resp)
    return resp


# ---------------------------------------------------------------- panel

def _proyecto(valor: str | None) -> str | None:
    return valor if valor and len(valor) <= 64 else None


@router.get("/panel", response_class=HTMLResponse)
async def panel_pagina(request: Request, proyecto: str | None = None, dias: int = 30):
    if not acceso.es_admin(request):
        return _ir("/login")
    if not db.enabled():
        return _pagina(request, "panel.html", {"sin_base": True, "proyectos": [], "filtros": {}})
    proyecto, dias = _proyecto(proyecto), panel.periodo_valido(dias)
    return _pagina(request, "panel.html", {
        "sin_base": False,
        "proyectos": await panel.proyectos(),
        "filtros": {"proyecto": proyecto or "", "dias": dias},
        "periodos": panel.PERIODOS,
        # Primera carga ya en el HTML: aparece sin esperar otra peticion.
        "resumen": await panel.resumen(proyecto, dias),
        "sesiones": await panel.sesiones_pagina(proyecto, dias, 1),
        "zona": settings.panel_timezone,
    })


@router.get("/panel/datos")
async def panel_datos(request: Request, proyecto: str | None = None, dias: int = 30):
    if not acceso.es_admin(request):
        return _no_autorizado()
    if not db.enabled():
        return _sin_base()
    return JSONResponse(await panel.resumen(_proyecto(proyecto), panel.periodo_valido(dias)), headers=_ENCABEZADOS)


@router.get("/panel/sesiones")
async def panel_sesiones(
    request: Request,
    proyecto: str | None = None,
    dias: int = 30,
    pagina: int = Query(1, ge=1, le=1_000_000),
    resultado: str | None = None,
    buscar: str | None = Query(None, max_length=100),
):
    if not acceso.es_admin(request):
        return _no_autorizado()
    if not db.enabled():
        return _sin_base()
    datos = await panel.sesiones_pagina(_proyecto(proyecto), panel.periodo_valido(dias), pagina,
                                        resultado if resultado in panel.RESULTADOS else None,
                                        (buscar or "").strip() or None)
    return JSONResponse(datos, headers=_ENCABEZADOS)


@router.get("/panel/sesion/{sesion_id}")
async def panel_sesion(request: Request, sesion_id: str):
    if not acceso.es_admin(request):
        return _no_autorizado()
    if not db.enabled():
        return _sin_base()
    datos = await panel.detalle_sesion(sesion_id) if len(sesion_id) <= 64 else None
    if datos is None:
        return JSONResponse({"detail": "No encontrado"}, status_code=404, headers=_ENCABEZADOS)
    return JSONResponse(datos, headers=_ENCABEZADOS)

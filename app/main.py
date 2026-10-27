import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.api.routes.identity import router as identity_router
from app.core.config import settings
from app.services import sesiones
from app.services.antispoof import load_antispoof_models
from app.services.asr_engine import load_model
from app.services.audio_liveness import verify_voice_from_path
from app.services.identity_verification import UPLOAD_DIR
from app.services.sesiones import FRASE_MAX_PALABRAS, FRASE_MIN_PALABRAS
from app.services.upload_io import ALLOWED_VIDEO_EXT, save_upload_to_temp
from app.services.face_engine import load_face_models
from app.services.face_track import load_track_models
from app.web import router as panel_router

templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger("uvicorn.error")


async def _mantenimiento() -> None:
    """Vence sesiones abandonadas y olvida las terminadas."""
    while True:
        try:
            await sesiones.mantenimiento()
        except Exception:
            logger.exception("Error en el mantenimiento de sesiones")
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Cargar aqui y no por request: los modelos tardan segundos en inicializar.
    load_model()
    load_face_models()
    load_antispoof_models()
    load_track_models()
    await db.connect()
    if db.enabled():
        await sesiones.recuperar_al_arrancar()
    if settings.demo:
        logger.warning("DEMO activo: pagina de prueba en / y API directa sin llave si no hay API_KEYS.")
    if not settings.admin_email or not settings.admin_password_hash:
        logger.warning("ADMIN_EMAIL / ADMIN_PASSWORD_HASH vacios: nadie puede entrar al panel.")
    tarea = asyncio.create_task(_mantenimiento())
    yield
    tarea.cancel()
    await db.close()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

if settings.cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
        # Para que un frontend pueda leer cuanto esperar tras un 429.
        expose_headers=["Retry-After"],
    )


@app.get("/health", include_in_schema=False)
def health():
    # Responde solo cuando el lifespan ya cargo los modelos.
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(request: Request):
    """Pagina de prueba interna con todos los detalles. En produccion (DEMO=false) lleva al panel."""
    if not settings.demo:
        return RedirectResponse("/panel", status_code=303)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "min_duration_sec": settings.audio_min_duration_sec,
            "max_duration_sec": settings.audio_max_duration_sec,
            "turn_offset": settings.gesture_turn_offset,
            "blink_closed": settings.gesture_blink_closed,
            "blink_open": settings.gesture_blink_open,
        },
    )


app.include_router(identity_router, prefix="/api")
app.include_router(sesiones.api)
app.include_router(sesiones.captura_api)
app.include_router(sesiones.paginas)
app.include_router(panel_router)


# Probador de frases (solo DEMO): para medir, con la voz de uno, que porcentaje
# saca una frase antes de usarla en las sesiones de un proyecto.
_AUDIO_EXT = ALLOWED_VIDEO_EXT | {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".aac"}


def _solo_demo() -> None:
    if not settings.demo:
        raise HTTPException(status_code=404, detail="No encontrado")


@app.get("/probar-frase", response_class=HTMLResponse, include_in_schema=False)
def probar_frase_pagina(request: Request):
    _solo_demo()
    return templates.TemplateResponse("probar_frase.html", {
        "request": request,
        "umbral": settings.phrase_match_threshold,
        "min_palabras": FRASE_MIN_PALABRAS,
        "max_palabras": FRASE_MAX_PALABRAS,
        "max_segundos": settings.audio_max_duration_sec,
    })


@app.post("/api/probar-frase", include_in_schema=False)
async def probar_frase(frase: str = Form(..., max_length=500), audio: UploadFile = File(...)):
    _solo_demo()
    path = await save_upload_to_temp(audio, UPLOAD_DIR, allowed_ext=_AUDIO_EXT, max_mb=settings.max_video_mb)
    try:
        return await verify_voice_from_path(path, frase)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

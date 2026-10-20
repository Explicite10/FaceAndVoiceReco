import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.routes.identity import router as identity_router
from app.core.config import settings
from app.services.antispoof import load_antispoof_models
from app.services.asr_engine import load_model
from app.services.face_engine import load_face_models
from app.services.face_track import load_track_models

templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Cargar aqui y no por request: los modelos tardan segundos en inicializar.
    load_model()
    load_face_models()
    load_antispoof_models()
    load_track_models()
    if settings.api_keys:
        logger.info("Acceso: API keys de %s", ", ".join(sorted(settings.api_keys)))
    else:
        logger.warning("Acceso: API_KEYS vacio, la API no pide autenticacion (solo para desarrollo).")
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
# Detector de gestos del navegador (Web Worker): asi no traba la vista de la camara.
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

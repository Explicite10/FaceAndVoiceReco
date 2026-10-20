import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import db
from app.api.routes.identity import router as identity_router
from app.core import acceso
from app.core.config import settings
from app.core.web_security import BodyLimit, SecurityHeaders
from app.services.antispoof import load_antispoof_models
from app.services.asr_engine import load_model
from app.services.face_engine import load_face_models
from app.services.face_track import load_track_models
from app.web import router as web_router

logger = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Cargar aqui y no por request: los modelos tardan segundos en inicializar.
    load_model()
    load_face_models()
    load_antispoof_models()
    load_track_models()
    await db.connect()
    acceso.set_qr_vigente(await db.qr_token())
    if not settings.admin_email or not settings.admin_password_hash:
        logger.warning("ADMIN_EMAIL / ADMIN_PASSWORD_HASH vacios: nadie puede entrar al panel.")
    yield
    await db.close()


# Sin /docs ni /openapi.json: en la demo no se publica el contrato de la API.
app = FastAPI(title=settings.app_name, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Detector de gestos del navegador (Web Worker), logo e iconos.
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
app.add_middleware(BodyLimit)
app.add_middleware(SecurityHeaders)


@app.get("/health", include_in_schema=False)
def health():
    # Responde solo cuando el lifespan ya cargo los modelos.
    return {"status": "ok"}


app.include_router(web_router)
app.include_router(identity_router, prefix="/api")

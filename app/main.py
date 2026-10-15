import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.api.routes.identity import router as identity_router
from app.core.config import settings
from app.services import sesiones
from app.services.antispoof import load_antispoof_models
from app.services.asr_engine import load_model
from app.services.face_engine import load_face_models
from app.services.face_track import load_track_models

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
    """Pagina de prueba interna con todos los detalles. En produccion (DEMO=false) no existe."""
    if not settings.demo:
        raise HTTPException(status_code=404, detail="No encontrado")
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

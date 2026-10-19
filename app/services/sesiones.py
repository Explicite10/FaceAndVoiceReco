"""Sesiones de prueba de vida para proyectos autorizados.

1. El backend del proyecto crea una sesion con la INE (POST /api/v1/sesiones)
   y manda al usuario a url_captura.
2. El usuario hace el reto (gestos + frase) en una sola grabacion; si no pasa,
   puede reintentar con un reto nuevo hasta max_intentos.
3. Al aprobar (o agotar intentos) se entrega el reporte JSON firmado al
   webhook del proyecto y el usuario regresa a url_retorno.

La INE solo vive en memoria como vector de 128 numeros mientras la sesion esta
activa, y el video solo mientras se analiza. En PostgreSQL queda el resumen
(estado, decision, checks fallidos, entrega), nunca datos biometricos. Si el
proyecto lo pide, el webhook trae ligas temporales al video, fotogramas y cara
de la INE del intento final; se borran al vencer o cuando el proyecto lo pide.
"""
import asyncio
import json
import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import numpy as np
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from app import db
from app.core.config import settings
from app.core.rate_limit import session_limiter
from app.core.security import require_project
from app.services import entrega, evidencia, marca, proyectos
from app.services.challenge import GESTURE_LABELS, consume_challenge, create_challenge, discard_challenge
from app.services.errors import CheckError
from app.services.face_service import read_as_np_rgb
from app.services.identity_verification import UPLOAD_DIR, analyze_video, embed_id_image
from app.services.report import build_summary, user_reasons
from app.services.text_norm import normalize_phrase
from app.services.upload_io import ALLOWED_VIDEO_EXT, save_upload_to_temp

logger = logging.getLogger("uvicorn.error")
templates = Jinja2Templates(directory="app/templates")

api = APIRouter(prefix="/api/v1/sesiones", tags=["Sesiones (backend del proyecto)"])
captura_api = APIRouter(prefix="/api/v1/captura", tags=["Captura (pagina del usuario)"], include_in_schema=False)
paginas = APIRouter(include_in_schema=False)

# pendiente -> procesando -> pendiente (reintento) | entregando -> completada | error_entrega
# o bien expirada / cancelada.
ACTIVOS = ("pendiente", "procesando", "error_entrega")
TERMINADOS = ("completada", "expirada", "cancelada")
# Tras terminar, la pagina del usuario aun puede consultar el estado un rato.
RECORDAR_MINUTOS = 60
# Tope de retos por sesion: pedir uno no gasta intentos.
MAX_RETOS = 20

_procesamiento = asyncio.Semaphore(max(1, settings.max_concurrent_verifications))


def ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(fecha: datetime | None) -> str | None:
    return fecha.isoformat(timespec="seconds") if fecha else None


@dataclass
class Sesion:
    id: str
    token: str
    proyecto_id: str
    proyecto_nombre: str
    webhook_secreto: str
    referencia_externa: str | None
    frase: str
    max_intentos: int
    umbral_rostro: float
    url_retorno: str | None
    webhook_url: str
    url_base: str
    metadata: dict
    dominios: list[str]
    creada: datetime
    expira: datetime
    estado: str = "pendiente"
    intentos: int = 0
    retos: int = 0
    aprobada: bool | None = None
    finalizada: datetime | None = None
    checks_fallidos: list[str] = field(default_factory=list)
    motivos: list[str] = field(default_factory=list)
    entrega: dict | None = None
    olvidar_en: datetime | None = None
    # Marca del proyecto en la pantalla (opcional).
    color_primario: str | None = None
    color_secundario: str | None = None
    logo: tuple[bytes, str] | None = field(default=None, repr=False)
    # Evidencia del intento final, por ligas temporales en el webhook (opcional).
    incluir_video: bool = False
    incluir_fotogramas: bool = False
    incluir_rostro_ine: bool = False
    # Solo en memoria y solo mientras hace falta.
    id_emb: np.ndarray | None = field(default=None, repr=False)
    reto_id: str | None = field(default=None, repr=False)
    reto: dict | None = field(default=None, repr=False)
    informe: dict | None = field(default=None, repr=False)
    rostro_ine: bytes | None = field(default=None, repr=False)
    # {nombre: {archivo, tipo, bytes, ...}} en disco hasta evidencia_expira.
    evidencia: dict | None = field(default=None, repr=False)
    evidencia_expira: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def olvidar_biometria(self) -> None:
        self.id_emb = None
        self.informe = None
        self.rostro_ine = None
        if self.reto_id:
            discard_challenge(self.reto_id)
        self.reto_id = self.reto = None

    @property
    def pide_evidencia(self) -> bool:
        return self.incluir_video or self.incluir_fotogramas or self.incluir_rostro_ine

    def borrar_evidencia(self) -> None:
        if self.evidencia is not None:
            evidencia.borrar(UPLOAD_DIR, self.id)
        self.evidencia = None


_sesiones: dict[str, Sesion] = {}
_por_token: dict[str, str] = {}


def _guardar_memoria(s: Sesion) -> None:
    _sesiones[s.id] = s
    _por_token[s.token] = s.id


def _quitar_memoria(s: Sesion) -> None:
    s.borrar_evidencia()
    _sesiones.pop(s.id, None)
    _por_token.pop(s.token, None)


# ---------------------------------------------------------------- base de datos

async def _persistir(s: Sesion) -> None:
    """Guarda el resumen de la sesion. Un fallo de la base no debe tumbar al usuario."""
    try:
        await db.pool().execute(
            """
            INSERT INTO sesiones (id, proyecto_id, referencia_externa, estado, aprobada, intentos, max_intentos,
                                  checks_fallidos, url_retorno, webhook_url, metadata, entrega, creada, expira, finalizada)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            ON CONFLICT (id) DO UPDATE SET
                estado = EXCLUDED.estado, aprobada = EXCLUDED.aprobada, intentos = EXCLUDED.intentos,
                checks_fallidos = EXCLUDED.checks_fallidos, entrega = EXCLUDED.entrega,
                expira = EXCLUDED.expira, finalizada = EXCLUDED.finalizada
            """,
            s.id, s.proyecto_id, s.referencia_externa, s.estado, s.aprobada, s.intentos, s.max_intentos,
            s.checks_fallidos, s.url_retorno, s.webhook_url, s.metadata, s.entrega, s.creada, s.expira, s.finalizada,
        )
    except Exception:
        logger.exception("No se pudo guardar la sesion %s", s.id)


async def _registrar_intento(s: Sesion, aprobado: bool, checks: list[str], duracion_ms: int | None) -> None:
    try:
        await db.pool().execute(
            "INSERT INTO intentos (sesion_id, numero, aprobado, checks_fallidos, duracion_ms) VALUES ($1, $2, $3, $4, $5)",
            s.id, s.intentos, aprobado, checks, duracion_ms,
        )
    except Exception:
        logger.exception("No se pudo guardar el intento %s de la sesion %s", s.intentos, s.id)


async def _intentos_de(sesion_id: str) -> list[dict]:
    rows = await db.pool().fetch(
        "SELECT numero, aprobado, checks_fallidos, duracion_ms, creado FROM intentos WHERE sesion_id = $1 ORDER BY numero",
        sesion_id,
    )
    return [{**dict(r), "creado": _iso(r["creado"])} for r in rows]


# ---------------------------------------------------------------- vistas

def _con_parametros(url: str, params: dict) -> str:
    p = urlparse(url)
    return urlunparse(p._replace(query=urlencode([*parse_qsl(p.query), *params.items()])))


def _redireccion(s: Sesion) -> str | None:
    if not s.url_retorno:
        return None
    return _con_parametros(s.url_retorno, {"sesion_id": s.id, "estado": s.estado, "aprobada": "1" if s.aprobada else "0"})


def _origen(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def vista(s: Sesion) -> dict:
    """Lo que ve el backend del proyecto."""
    return {
        "id": s.id,
        "referencia_externa": s.referencia_externa,
        "frase": s.frase,
        "estado": s.estado,
        "aprobada": s.aprobada,
        "intentos": s.intentos,
        "max_intentos": s.max_intentos,
        "checks_fallidos": s.checks_fallidos,
        "url_captura": f"{s.url_base}/captura/{s.token}" if s.estado in ACTIVOS else None,
        "creada": _iso(s.creada),
        "expira": _iso(s.expira),
        "finalizada": _iso(s.finalizada),
        "entrega": s.entrega,
        "metadata": s.metadata,
    }


def _vista_db(row) -> dict:
    return {
        "id": row["id"],
        "referencia_externa": row["referencia_externa"],
        "frase": None,  # no se guarda en la base
        "estado": row["estado"],
        "aprobada": row["aprobada"],
        "intentos": row["intentos"],
        "max_intentos": row["max_intentos"],
        "checks_fallidos": list(row["checks_fallidos"]),
        "url_captura": None,
        "creada": _iso(row["creada"]),
        "expira": _iso(row["expira"]),
        "finalizada": _iso(row["finalizada"]),
        "entrega": row["entrega"],
        "metadata": row["metadata"],
    }


def _config_publica(s: Sesion) -> dict:
    """Lo que ve la pagina del usuario: sin datos tecnicos."""
    return {
        "sesion_id": s.id,
        "proyecto": s.proyecto_nombre,
        "estado": s.estado,
        "aprobada": s.aprobada,
        "intentos": s.intentos,
        "intentos_restantes": max(0, s.max_intentos - s.intentos),
        "motivos": s.motivos,
        "redireccion": _redireccion(s) if s.estado in TERMINADOS else None,
        "origen_retorno": _origen(s.url_retorno),
    }


def _sesion_por_token(token: str) -> Sesion:
    s = _sesiones.get(_por_token.get(token, ""))
    if not s:
        raise HTTPException(status_code=404, detail="El enlace no es válido o ya venció.")
    return s


# ---------------------------------------------------------------- ciclo de vida

async def _terminar_sin_resultado(s: Sesion, estado: str) -> None:
    """Expirada o cancelada: se descarta la INE y se avisa al proyecto (sin reporte, un intento)."""
    s.olvidar_biometria()
    s.borrar_evidencia()  # sus ligas nunca se entregaron
    s.estado = estado
    s.finalizada = ahora()
    s.olvidar_en = ahora() + timedelta(minutes=RECORDAR_MINUTOS)
    await _persistir(s)
    asyncio.get_running_loop().create_task(_avisar_fin_sin_resultado(s, estado))


async def _avisar_fin_sin_resultado(s: Sesion, estado: str) -> None:
    s.entrega = await entrega.enviar(s.webhook_url, s.webhook_secreto, f"sesion.{estado}", {"sesion": vista(s)}, reintentos=0)
    await _persistir(s)


async def finalizar(s: Sesion) -> None:
    """Entrega el reporte al proyecto y, si lo confirma, olvida la INE."""
    s.aprobada = bool(s.informe and s.informe["result"]["approved"])
    s.estado = "entregando"
    finalizada = ahora()
    resumen = None
    if s.informe:
        try:
            resumen = build_summary(s.informe)
        except Exception:
            # El reporte completo va de todos modos; un resumen roto no debe frenar la entrega.
            logger.exception("No se pudo armar el resumen de la sesion %s", s.id)
    datos = {
        "sesion": {**vista(s), "estado": "completada", "url_captura": None, "finalizada": _iso(finalizada)},
        "resumen": resumen,
        "resultado": s.informe,
        "intentos": await _intentos_de(s.id),
        "evidencia": _evidencia_publica(s) if s.pide_evidencia else None,
    }
    resultado = await entrega.enviar(s.webhook_url, s.webhook_secreto, "sesion.completada", datos)
    s.entrega = {k: resultado[k] for k in ("ok", "codigo", "error", "intentos", "id")}
    if resultado["ok"]:
        s.olvidar_biometria()
        s.estado = "completada"
        s.finalizada = finalizada
        s.olvidar_en = ahora() + timedelta(minutes=RECORDAR_MINUTOS)
        logger.info("Sesion %s entregada (aprobada=%s)", s.id, s.aprobada)
    else:
        # El reporte se conserva hasta que venza la sesion, para reintentar el envio.
        s.estado = "error_entrega"
    await _persistir(s)


async def _procesar(s: Sesion, video_path: str, reto: dict, ventana: tuple[float, float | None] | None = None) -> None:
    t0 = time.time()
    informe = None
    datos_evidencia = None
    try:
        async with _procesamiento:
            informe = await analyze_video(video_path, s.id_emb, reto["phrase"], s.umbral_rostro, reto, t0,
                                          ventana_frase=ventana)
            # Solo el intento que decide la sesion: los anteriores no se entregan.
            final = bool(informe and informe["result"]["approved"]) or s.intentos >= s.max_intentos
            if final and s.pide_evidencia:
                try:
                    datos_evidencia = await asyncio.to_thread(
                        evidencia.guardar, UPLOAD_DIR, s.id, video_path, s.incluir_video,
                        s.incluir_fotogramas, s.rostro_ine if s.incluir_rostro_ine else None,
                    )
                except Exception:
                    logger.exception("No se pudo armar la evidencia de la sesion %s", s.id)
                    evidencia.borrar(UPLOAD_DIR, s.id)
    except Exception:
        logger.exception("Fallo el analisis de la sesion %s", s.id)
    finally:
        try:
            os.remove(video_path)
        except OSError:
            pass

    async with s.lock:
        if s.estado != "procesando":  # cancelada o expirada mientras se analizaba
            if datos_evidencia is not None:
                evidencia.borrar(UPLOAD_DIR, s.id)
            return
        aprobado = bool(informe and informe["result"]["approved"])
        s.checks_fallidos = informe["result"]["failed_checks"] if informe else ["internal.analysis_error"]
        await _registrar_intento(s, aprobado, s.checks_fallidos, informe["processing_time_ms"] if informe else None)
        # Tambien si se agotan los intentos: la pantalla final dice por que no paso.
        s.motivos = user_reasons(informe)
        if aprobado or s.intentos >= s.max_intentos:
            s.informe = informe
            if datos_evidencia is not None:
                s.evidencia = datos_evidencia
                s.evidencia_expira = ahora() + timedelta(minutes=settings.evidence_minutes)
            await finalizar(s)
            return
        s.estado = "pendiente"
        await _persistir(s)


async def mantenimiento() -> None:
    """Vence sesiones abandonadas y olvida las terminadas. Corre cada pocos segundos."""
    for s in list(_sesiones.values()):
        if s.estado in ACTIVOS and s.estado != "procesando" and s.expira < ahora():
            async with s.lock:
                if s.estado in ACTIVOS and s.estado != "procesando":
                    await _terminar_sin_resultado(s, "expirada")
        elif s.estado in TERMINADOS and s.olvidar_en and s.olvidar_en < ahora() and s.evidencia is None:
            _quitar_memoria(s)
        if s.evidencia is not None and s.evidencia_expira and s.evidencia_expira < ahora():
            s.borrar_evidencia()


async def recuperar_al_arrancar() -> None:
    """Sesiones activas de antes de un reinicio: su INE estaba solo en memoria, no se pueden terminar."""
    evidencia.borrar_todo(UPLOAD_DIR)  # sus ligas ya no tienen sesion en memoria
    rows = await db.pool().fetch(
        "UPDATE sesiones SET estado = 'expirada', finalizada = now() WHERE estado = ANY($1::text[]) RETURNING *",
        [*ACTIVOS, "entregando"],
    )
    for row in rows:
        proyecto = await proyectos.por_id(row["proyecto_id"])
        if proyecto:
            asyncio.get_running_loop().create_task(
                entrega.enviar(row["webhook_url"], proyecto["webhook_secreto"], "sesion.expirada",
                               {"sesion": _vista_db(row)}, reintentos=0)
            )
    if rows:
        logger.warning("%d sesiones activas se marcaron como expiradas por el reinicio", len(rows))


# ---------------------------------------------------------------- API para el backend del proyecto

async def _leer_ine(ine: UploadFile) -> np.ndarray:
    # El formato se valida por el contenido real, no por el content-type: los
    # backends suelen mandar application/octet-stream.
    return await read_as_np_rgb(ine, "ine_imagen")


def _leer_metadata(texto: str | None) -> dict:
    if not texto:
        return {}
    if len(texto.encode()) > 4096:
        raise HTTPException(status_code=422, detail="metadata no debe exceder 4 KB.")
    try:
        valor = json.loads(texto)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="metadata debe ser un objeto JSON.")
    if not isinstance(valor, dict):
        raise HTTPException(status_code=422, detail="metadata debe ser un objeto JSON.")
    return valor


# La frase se lee despues de los gestos y todo debe caber en el video (max 60 s).
FRASE_MIN_PALABRAS, FRASE_MAX_PALABRAS = 4, 50


def _validar_frase(frase: str) -> str:
    frase = " ".join(frase.split())
    palabras = len(normalize_phrase(frase).split())
    if not FRASE_MIN_PALABRAS <= palabras <= FRASE_MAX_PALABRAS:
        raise HTTPException(
            status_code=422,
            detail=f"frase debe tener entre {FRASE_MIN_PALABRAS} y {FRASE_MAX_PALABRAS} palabras (tiene {palabras}).",
        )
    return frase


def _requiere_db() -> None:
    if not db.enabled():
        raise HTTPException(status_code=503, detail="Las sesiones no están disponibles: falta configurar DATABASE_URL.")


@api.post("", status_code=201, summary="Crear una sesion de prueba de vida",
          dependencies=[Depends(_requiere_db)])
async def crear_sesion(
    request: Request,
    ine_imagen: UploadFile = File(..., description="Foto de la INE (JPEG, PNG o WEBP, max 10 MB), como archivo."),
    frase: str = Form(..., max_length=500, description="Frase que el usuario lee en voz alta, de 4 a 50 palabras."),
    referencia_externa: str | None = Form(None, max_length=200, description="Tu identificador; vuelve en el aviso"),
    url_retorno: str | None = Form(None, description="A donde regresa el usuario al terminar"),
    webhook_url: str | None = Form(None, description="Donde se entrega el resultado; si no, la del proyecto"),
    max_intentos: int | None = Form(None, ge=1, le=5, description="Intentos del usuario. Por defecto 3."),
    expira_minutos: int | None = Form(None, ge=5, le=1440, description="Vigencia del enlace. Por defecto 30."),
    umbral_rostro: float | None = Form(None, ge=1, le=100, description="% minimo de similitud con la INE. Por defecto 70."),
    metadata: str | None = Form(None, description='Objeto JSON que vuelve en el aviso (max 4 KB), ej. {"tramite": "alta"}'),
    logo: UploadFile | None = File(None, description="Logo para la pantalla (PNG, JPEG o WEBP, max 1 MB)."),
    color_primario: str | None = Form(None, description="Hexadecimal, ej. #1B56D6: botones y paso actual."),
    color_secundario: str | None = Form(None, description="Hexadecimal: pasos completados y carga."),
    incluir_video: bool = Form(False, description="El webhook trae una liga temporal al video (mp4) del intento final."),
    incluir_fotogramas: bool = Form(False, description="El webhook trae ligas a 3 fotogramas JPEG de frente del intento final."),
    incluir_rostro_ine: bool = Form(False, description="El webhook trae una liga al recorte de la cara de la INE."),
    proyecto: dict = Depends(require_project),
):
    session_limiter(request)
    frase = _validar_frase(frase)
    datos_metadata = _leer_metadata(metadata)
    primario = marca.color(color_primario, "color_primario")
    secundario = marca.color(color_secundario, "color_secundario")
    datos_logo = await marca.logo(logo)
    webhook_url = webhook_url or proyecto["webhook_url"]
    if not webhook_url:
        raise HTTPException(status_code=422, detail="Falta webhook_url: es el único canal por el que llega el resultado.")
    for campo, url in (("url_retorno", url_retorno), ("webhook_url", webhook_url)):
        if url:
            proyectos.validar_url(url, proyecto, campo)

    # La cara de la INE se revisa ya: no tiene caso mandar al usuario a grabar
    # si no se va a poder comparar. La imagen se descarta aqui mismo.
    np_ine = await _leer_ine(ine_imagen)
    try:
        id_emb = await embed_id_image(np_ine)
    except CheckError as exc:
        raise HTTPException(status_code=422, detail="No se detectó un rostro en ine_imagen.") from exc
    # Solo el recorte de la cara, y solo si se pidio: el resto de la INE se descarta.
    rostro_ine = await asyncio.to_thread(evidencia.recorte_rostro, np_ine) if incluir_rostro_ine else None
    del np_ine

    s = Sesion(
        id="ses_" + uuid.uuid4().hex,
        token=secrets.token_urlsafe(32),
        proyecto_id=proyecto["id"],
        proyecto_nombre=proyecto["nombre"],
        webhook_secreto=proyecto["webhook_secreto"],
        referencia_externa=referencia_externa,
        frase=frase,
        max_intentos=max_intentos or settings.session_max_attempts,
        umbral_rostro=umbral_rostro or settings.face_match_percent,
        url_retorno=url_retorno,
        webhook_url=webhook_url,
        url_base=settings.public_url or str(request.base_url).rstrip("/"),
        metadata=datos_metadata,
        dominios=list(proyecto["dominios_permitidos"]),
        creada=ahora(),
        expira=ahora() + timedelta(minutes=expira_minutos or settings.session_minutes),
        id_emb=id_emb,
        color_primario=primario,
        color_secundario=secundario,
        logo=datos_logo,
        incluir_video=incluir_video,
        incluir_fotogramas=incluir_fotogramas,
        incluir_rostro_ine=incluir_rostro_ine,
        rostro_ine=rostro_ine,
    )
    await _persistir(s)
    _guardar_memoria(s)
    return vista(s)


@api.get("", summary="Listar las sesiones del proyecto", dependencies=[Depends(_requiere_db)])
async def listar_sesiones(estado: str | None = None, limite: int = 50, proyecto: dict = Depends(require_project)):
    limite = max(1, min(limite, 500))
    rows = await db.pool().fetch(
        "SELECT * FROM sesiones WHERE proyecto_id = $1 AND ($2::text IS NULL OR estado = $2)"
        " ORDER BY creada DESC LIMIT $3",
        proyecto["id"], estado, limite,
    )
    return [vista(_sesiones[r["id"]]) if r["id"] in _sesiones else _vista_db(r) for r in rows]


@api.get("/{sesion_id}", summary="Consultar una sesion", dependencies=[Depends(_requiere_db)])
async def obtener_sesion(sesion_id: str, proyecto: dict = Depends(require_project)):
    s = _sesiones.get(sesion_id)
    if s and s.proyecto_id == proyecto["id"]:
        return {**vista(s), "intentos_detalle": await _intentos_de(s.id)}
    row = await db.pool().fetchrow("SELECT * FROM sesiones WHERE id = $1 AND proyecto_id = $2", sesion_id, proyecto["id"])
    if not row:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
    return {**_vista_db(row), "intentos_detalle": await _intentos_de(sesion_id)}


@api.post("/{sesion_id}/cancelar", summary="Cancelar una sesion activa", dependencies=[Depends(_requiere_db)])
async def cancelar(sesion_id: str, proyecto: dict = Depends(require_project)):
    s = _sesiones.get(sesion_id)
    if not s or s.proyecto_id != proyecto["id"]:
        raise HTTPException(status_code=404, detail="Sesión no encontrada o ya terminada.")
    async with s.lock:
        if s.estado not in ACTIVOS:
            raise HTTPException(status_code=409, detail=f"La sesión ya está {s.estado}.")
        await _terminar_sin_resultado(s, "cancelada")
    return vista(s)


# ---------------------------------------------------------------- evidencia (ligas temporales)

def _evidencia_publica(s: Sesion) -> dict:
    """El bloque `evidencia` del webhook: ligas, no los archivos."""
    archivos = s.evidencia or {}
    base = f"{s.url_base}/api/v1/sesiones/{s.id}/evidencia/"

    def liga(nombre: str) -> dict:
        a = archivos[nombre]
        return {"url": base + nombre, "tipo": a["tipo"], "bytes": a["bytes"]}

    return {
        "intento": s.intentos,
        "expira": _iso(s.evidencia_expira) if s.evidencia is not None else None,
        "video": liga("video") if "video" in archivos else None,
        "fotogramas": [
            {"orden": a["orden"], "segundo": a["segundo"], "de_frente": a["de_frente"], **liga(nombre)}
            for nombre, a in archivos.items() if nombre.startswith("fotograma/")
        ],
        "rostro_ine": liga("rostro-ine") if "rostro-ine" in archivos else None,
    }


def _evidencia_de(sesion_id: str, proyecto: dict) -> Sesion:
    s = _sesiones.get(sesion_id)
    if not s or s.proyecto_id != proyecto["id"]:
        raise HTTPException(status_code=404, detail="Sesión no encontrada.")
    if s.evidencia is None:
        raise HTTPException(status_code=410, detail="La evidencia ya no está disponible: venció o se borró.")
    return s


def _descargar(sesion_id: str, nombre: str, proyecto: dict) -> FileResponse:
    s = _evidencia_de(sesion_id, proyecto)
    a = s.evidencia.get(nombre)
    if not a:
        raise HTTPException(status_code=404, detail="Ese archivo no forma parte de la evidencia.")
    descarga = f"{s.id}_{os.path.basename(a['archivo'])}"
    return FileResponse(a["archivo"], media_type=a["tipo"], filename=descarga,
                        headers={"Cache-Control": "no-store"})


@api.get("/{sesion_id}/evidencia/video", summary="Descargar el video del intento final")
async def evidencia_video(sesion_id: str, proyecto: dict = Depends(require_project)):
    return _descargar(sesion_id, "video", proyecto)


@api.get("/{sesion_id}/evidencia/fotograma/{orden}", summary="Descargar un fotograma del intento final")
async def evidencia_fotograma(sesion_id: str, orden: int, proyecto: dict = Depends(require_project)):
    return _descargar(sesion_id, f"fotograma/{orden}", proyecto)


@api.get("/{sesion_id}/evidencia/rostro-ine", summary="Descargar el recorte de la cara de la INE")
async def evidencia_rostro_ine(sesion_id: str, proyecto: dict = Depends(require_project)):
    return _descargar(sesion_id, "rostro-ine", proyecto)


@api.delete("/{sesion_id}/evidencia", status_code=204, summary="Borrar la evidencia ya descargada")
async def borrar_evidencia(sesion_id: str, proyecto: dict = Depends(require_project)):
    _evidencia_de(sesion_id, proyecto).borrar_evidencia()
    return Response(status_code=204)


# ---------------------------------------------------------------- API de la pagina del usuario (por token)

@captura_api.get("/{token}")
async def config_captura(token: str):
    return _config_publica(_sesion_por_token(token))


@captura_api.get("/{token}/logo")
async def logo_captura(token: str):
    s = _sesion_por_token(token)
    if not s.logo:
        raise HTTPException(status_code=404, detail="La sesión no tiene logo.")
    contenido, tipo = s.logo
    return Response(contenido, media_type=tipo, headers={
        "Cache-Control": "private, max-age=3600",
        "X-Content-Type-Options": "nosniff",
    })


@captura_api.post("/{token}/reto")
async def nuevo_reto(token: str):
    """Gestos y frase para el siguiente intento. Pedir otro invalida el anterior."""
    s = _sesion_por_token(token)
    async with s.lock:
        if s.estado != "pendiente" or s.expira < ahora():
            raise HTTPException(status_code=409, detail="La sesión ya no acepta intentos.")
        if s.retos >= MAX_RETOS:
            raise HTTPException(status_code=429, detail="Demasiados intentos. Pide un enlace nuevo.")
        if s.reto_id:
            discard_challenge(s.reto_id)
        # Gestos nuevos en cada intento; la frase es la que mando el proyecto.
        s.reto = create_challenge(client=s.id, phrase=s.frase)
        s.reto_id = s.reto["challenge_id"]
        s.retos += 1
    return {
        "phrase": s.reto["phrase"],
        "gestures": [{"gesture": g, "instruction": GESTURE_LABELS[g]} for g in s.reto["gestures"]],
        "expires_in_sec": settings.challenge_ttl_sec,
    }


@captura_api.post("/{token}/video", status_code=202)
async def subir_video(
    token: str,
    video: UploadFile = File(...),
    # Segundos del video en que aparecio la frase y en que termino (los mide la pagina).
    frase_inicio: float | None = Form(None, ge=0, le=600),
    frase_fin: float | None = Form(None, ge=0, le=600),
):
    """Recibe la grabacion del reto vigente; se analiza en segundo plano."""
    s = _sesion_por_token(token)
    async with s.lock:
        if s.estado != "pendiente" or s.expira < ahora():
            raise HTTPException(status_code=409, detail="La sesión ya no acepta intentos.")
        if not s.reto_id:
            raise HTTPException(status_code=409, detail="Primero pide un reto.")
        reto = consume_challenge(s.reto_id, s.id)  # 400 si ya vencio
        s.reto_id = s.reto = None
        path = await save_upload_to_temp(video, UPLOAD_DIR, allowed_ext=ALLOWED_VIDEO_EXT, max_mb=settings.max_video_mb)
        s.intentos += 1
        s.estado = "procesando"
        s.motivos = []
        await _persistir(s)
    ventana = (frase_inicio, frase_fin) if frase_inicio is not None else None
    asyncio.get_running_loop().create_task(_procesar(s, path, reto, ventana))
    return _config_publica(s)


@captura_api.post("/{token}/reintentar-entrega")
async def reintentar_entrega(token: str):
    s = _sesion_por_token(token)
    async with s.lock:
        if s.estado != "error_entrega":
            raise HTTPException(status_code=409, detail="No hay un envío pendiente.")
        await finalizar(s)
    return _config_publica(s)


# ---------------------------------------------------------------- pagina

@paginas.get("/captura/{token}", response_class=HTMLResponse)
async def pagina_captura(request: Request, token: str):
    s = _sesiones.get(_por_token.get(token, ""))
    ancestros = ["'self'"]
    for d in (s.dominios if s else []):
        ancestros += [f"https://{d}", f"https://*.{d}"]
    return templates.TemplateResponse(
        "captura.html",
        {
            "request": request,
            "min_duration_sec": settings.audio_min_duration_sec,
            "max_duration_sec": settings.audio_max_duration_sec,
            "turn_offset": settings.gesture_turn_offset,
            "blink_closed": settings.gesture_blink_closed,
            "blink_open": settings.gesture_blink_open,
            # Colores ya validados como hexadecimal: se escriben tal cual en el CSS.
            "marca": {
                "proyecto": s.proyecto_nombre if s else "",
                "logo_url": f"/api/v1/captura/{token}/logo" if s and s.logo else None,
                "primario": s.color_primario if s else None,
                "texto_primario": marca.texto_sobre(s.color_primario) if s and s.color_primario else None,
                "secundario": s.color_secundario if s else None,
            },
        },
        headers={
            # Solo los dominios del proyecto pueden incrustar la captura en un iframe.
            "Content-Security-Policy": f"frame-ancestors {' '.join(ancestros)}",
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
        },
    )

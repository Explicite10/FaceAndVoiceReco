"""Laboratorio local (LAB=true): la misma prueba de vida que usan los proyectos,
en la maquina de uno, para revisar casos que fallan.

En la computadora se sube la INE y /lab/resultado/{id} muestra un QR. La
persona lo escanea con su telefono y hace la captura real (/captura/{token});
cada intento pasa por el mismo analisis que en produccion (gestos, frase, rostro
y prueba de vida, con los mismos umbrales). Lo unico distinto: no hay proyecto,
base ni webhook. La pagina de la computadora se actualiza sola con el reporte de
cada intento y un diagnostico de la foto de la INE.

Cada prueba se guarda como JSON en pruebas_lab/ (sin imagenes). La INE y los
recortes solo viven en memoria hasta reiniciar.
"""
import asyncio
import io
import ipaddress
import json
import os
import re
import secrets
import uuid
from datetime import timedelta

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.core.config import settings
from app.services import evidencia, marca, sesiones
from app.services.errors import CheckError
from app.services.face_engine import MAX_GIRO_FRENTE, detect_faces, pick_best_face, pose
from app.services.identity_verification import embed_id_image
from app.services.image_quality import face_quality, medir_cara
from app.services.report import build_summary
from app.services.upload_io import read_upload_limited

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARPETA = os.path.join(_RAIZ, "pruebas_lab")
templates = Jinja2Templates(directory="app/templates")


def _solo_local(request: Request) -> None:
    # La camara solo funciona sin HTTPS en localhost, y el lab no tiene llave.
    host = request.client.host if request.client else ""
    try:
        local = ipaddress.ip_address(host).is_loopback
    except ValueError:
        local = host in ("localhost", "testclient")
    if not local:
        raise HTTPException(status_code=404, detail="No encontrado")


router = APIRouter(prefix="/lab", include_in_schema=False, dependencies=[Depends(_solo_local)])

# {sesion_id: {etiqueta, params, ine (bytes originales), rostro_ine (jpeg), diagnostico, archivo}}
_corridas: dict[str, dict] = {}


def url_telefono(request: Request) -> str:
    """A donde apunta el QR: el puerto HTTPS de la red local que abre scripts/lab.py."""
    return os.getenv("LAB_PHONE_URL") or str(request.base_url).rstrip("/")


def _qr(url: str) -> str:
    import segno  # solo en el laboratorio (requirements-dev)

    return segno.make(url, error="m").svg_inline(scale=6, border=2)


def _umbrales() -> dict:
    return {
        "rostro_ine_porcentaje": settings.face_match_percent,
        "misma_persona_porcentaje": settings.same_person_percent,
        "frase_porcentaje": settings.phrase_match_threshold,
        "persona_real_probabilidad": settings.antispoof_threshold,
        "similitud_coseno_al_50": settings.sim_threshold,
        "intentos_por_sesion": settings.session_max_attempts,
        "modelo_voz": f"{settings.asr_model_size} ({settings.asr_device})",
        "limpieza_audio": settings.audio_clean,
        "frase_como_contexto": settings.asr_initial_prompt,
    }


def diagnostico_ine(rgb) -> dict:
    """Que tan buena es la cara de la INE para compararla (lo que ve el modelo)."""
    h, w = rgb.shape[:2]
    caras = detect_faces(rgb)
    cara = pick_best_face(caras)
    avisos = []
    datos = {"imagen_px": f"{w} x {h}", "caras_detectadas": len(caras), "cara": None, "avisos": avisos}
    if cara is None:
        avisos.append("No se detectó una cara en la INE.")
        return datos
    q = face_quality(rgb, cara)
    giro, inclinacion = pose(cara.row)
    datos["cara"] = {**q, "giro": round(giro, 3), "inclinacion": round(inclinacion, 3)}
    if q["size_px"] < settings.photo_min_face_px:
        avisos.append(f"La cara mide {q['size_px']} px; el modelo trabaja a {settings.photo_min_face_px} px. "
                      "Una foto de la INE más grande o más cerca suele subir el porcentaje.")
    if q["sharpness"] < settings.photo_min_sharpness:
        avisos.append(f"La cara está borrosa (nitidez {q['sharpness']}; mínimo sugerido {settings.photo_min_sharpness:g}).")
    if q["brightness"] < settings.photo_min_brightness:
        avisos.append(f"La cara está oscura (brillo {q['brightness']}; mínimo sugerido {settings.photo_min_brightness:g}).")
    if q["brightness"] > 220:
        avisos.append(f"La cara está muy clara o con reflejo (brillo {q['brightness']}).")
    if q["detector_score"] < 0.8:
        avisos.append(f"El detector no está seguro de la cara ({q['detector_score']}): puede ser reflejo, holograma o recorte.")
    if giro > MAX_GIRO_FRENTE:
        avisos.append("La cara de la INE no se ve de frente (está girada o la credencial está inclinada).")
    if len(caras) > 1:
        avisos.append(f"Se detectaron {len(caras)} caras en la INE (la foto fantasma); se usa la más grande.")
    return datos


def _archivo(sid: str, etiqueta: str) -> str:
    nombre = re.sub(r"[^a-zA-Z0-9_-]+", "-", etiqueta).strip("-")[:40] or "prueba"
    return os.path.join(CARPETA, f"{sesiones.ahora():%Y%m%d-%H%M%S}_{nombre}_{sid[-8:]}.json")


def _guardar(s: sesiones.Sesion) -> None:
    c = _corridas.get(s.id)
    if not c:
        return
    os.makedirs(CARPETA, exist_ok=True)
    datos = {
        "sesion_id": s.id,
        "etiqueta": c["etiqueta"],
        "creada": sesiones._iso(s.creada),
        "estado": s.estado,
        "aprobada": s.aprobada,
        "frase": s.frase,
        "umbral_rostro": s.umbral_rostro,
        "umbrales": _umbrales(),
        "ine": c["diagnostico"],
        "intentos": s.historial,
    }
    with open(c["archivo"], "w", encoding="utf-8") as fh:
        json.dump(datos, fh, ensure_ascii=False, indent=2)


async def _crear(request: Request, etiqueta: str, ine: bytes, nombre_archivo: str, frase: str,
                 umbral: float, max_intentos: int, apariencia: dict | None = None) -> sesiones.Sesion:
    """`apariencia`: titulo, color_primario, color_secundario y logo (bytes, tipo), ya validados."""
    apariencia = apariencia or {}
    # La INE pasa por la misma lectura que en produccion (EXIF, formato, enderezar).
    np_ine = await sesiones._leer_ine(UploadFile(file=io.BytesIO(ine), filename=nombre_archivo))
    diagnostico = await asyncio.to_thread(diagnostico_ine, np_ine)
    try:
        id_emb = await embed_id_image(np_ine)
    except CheckError as exc:
        raise HTTPException(status_code=422, detail="No se detectó un rostro en la INE.") from exc
    rostro_ine = await asyncio.to_thread(evidencia.recorte_rostro, np_ine)
    medidas_ine = await asyncio.to_thread(medir_cara, np_ine)

    sid = "ses_" + uuid.uuid4().hex
    s = sesiones.Sesion(
        id=sid,
        token=secrets.token_urlsafe(32),
        proyecto_id="lab",
        proyecto_nombre="Laboratorio",
        webhook_secreto="",
        referencia_externa=etiqueta,
        frase=frase,
        max_intentos=max_intentos,
        umbral_rostro=umbral,
        # El telefono se queda en la pantalla final: el resultado se ve en la computadora.
        url_retorno=None,
        webhook_url="",
        url_base=url_telefono(request),
        metadata={},
        dominios=[],
        creada=sesiones.ahora(),
        expira=sesiones.ahora() + timedelta(minutes=settings.session_minutes),
        id_emb=id_emb,
        # Fotogramas de frente del intento final, para verlos junto a la INE.
        incluir_fotogramas=True,
        local=True,
        al_registrar=_guardar,
        medidas_ine=medidas_ine,
        titulo=apariencia.get("titulo"),
        color_primario=apariencia.get("color_primario"),
        color_secundario=apariencia.get("color_secundario"),
        logo=apariencia.get("logo"),
    )
    _corridas[sid] = {
        "etiqueta": etiqueta,
        "params": {"frase": frase, "umbral": umbral, "max_intentos": max_intentos, "apariencia": apariencia},
        "ine": ine,
        "nombre_archivo": nombre_archivo,
        "rostro_ine": rostro_ine,
        "diagnostico": diagnostico,
        "archivo": _archivo(sid, etiqueta),
    }
    sesiones._guardar_memoria(s)
    _guardar(s)
    return s


# ---------------------------------------------------------------- vistas

def _intento(i: dict) -> dict:
    """Lo importante de un intento para leerlo de un vistazo."""
    r = (i.get("respuesta") or {}).get("reporte")
    resumen = None
    if r:
        try:
            resumen = build_summary(r)
        except Exception:
            resumen = None
    cara = (r or {}).get("details", {}).get("face") or {}
    voz = ((r or {}).get("details", {}).get("voice") or {}).get("phrase_match") or {}
    # Tamano de la cara en cada tramo (fuera del reporte, en "medidas").
    medidas = (i.get("respuesta") or {}).get("medidas") or {}
    video = {m["tramo"]: {"cara_px": m.get("cara_px"), "nitidez": m.get("nitidez")}
             for m in medidas.get("video", {}).get("tramos", [])}
    return {
        **i,
        "motivos": (i.get("respuesta") or {}).get("motivos") or [],
        "texto": (r or {}).get("result", {}).get("summary"),
        "resumen": resumen,
        "tramos": [{**t, **video.get(t["index"], {})} for t in cara.get("segments") or []],
        "video_px": medidas.get("video", {}).get("resolucion"),
        "audio": medidas.get("audio"),
        "pct_voz": voz.get("match_percent"),
        "errores": (r or {}).get("errors") or [],
        "json": json.dumps(r if r is not None else i.get("respuesta"), ensure_ascii=False, indent=2),
    }


def _previas() -> list[dict]:
    if not os.path.isdir(CARPETA):
        return []
    filas = []
    for nombre in sorted(os.listdir(CARPETA), reverse=True)[:50]:
        try:
            with open(os.path.join(CARPETA, nombre), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        ultimo = d["intentos"][-1] if d.get("intentos") else None
        pct = pct_voz = None
        if ultimo and (ultimo.get("respuesta") or {}).get("reporte"):
            det = ultimo["respuesta"]["reporte"].get("details") or {}
            pct = (det.get("face") or {}).get("similarity", {}).get("average_percent")
            pct_voz = ((det.get("voice") or {}).get("phrase_match") or {}).get("match_percent")
        filas.append({"archivo": nombre, "id": d["sesion_id"], "etiqueta": d.get("etiqueta"), "creada": d.get("creada"),
                      "estado": d.get("estado"), "aprobada": d.get("aprobada"), "intentos": len(d.get("intentos") or []),
                      "pct": pct, "pct_voz": pct_voz, "en_memoria": d["sesion_id"] in _corridas})
    return filas


def _pagina_inicio(request: Request, error: str | None = None, frase: str | None = None, etiqueta: str = ""):
    return templates.TemplateResponse("lab.html", {
        "request": request,
        "umbrales": _umbrales(),
        "frase": frase or settings.expected_phrase,
        "etiqueta": etiqueta,
        "error": error,
        "min_palabras": sesiones.FRASE_MIN_PALABRAS,
        "max_palabras": sesiones.FRASE_MAX_PALABRAS,
        "previas": _previas(),
        "carpeta": CARPETA,
    }, status_code=422 if error else 200)


@router.get("", response_class=HTMLResponse)
async def inicio(request: Request):
    return _pagina_inicio(request)


@router.post("")
async def nueva(
    request: Request,
    ine_imagen: UploadFile = File(...),
    etiqueta: str = Form("", max_length=80),
    frase: str = Form(..., max_length=500),
    umbral_rostro: float = Form(settings.face_match_percent, ge=1, le=100),
    max_intentos: int = Form(settings.session_max_attempts, ge=1, le=5),
    titulo: str = Form("", max_length=200),
    color_primario: str = Form(""),
    color_secundario: str = Form(""),
    logo: UploadFile | None = File(None),
):
    try:
        frase = sesiones._validar_frase(frase)
        datos = await read_upload_limited(ine_imagen, settings.max_image_mb, "'ine_imagen'")
        # Misma validacion de marca que el endpoint de sesiones.
        apariencia = {
            "titulo": marca.titulo(titulo),
            "color_primario": marca.color(color_primario, "color_primario"),
            "color_secundario": marca.color(color_secundario, "color_secundario"),
            "logo": await marca.logo(logo),
        }
        s = await _crear(request, etiqueta.strip() or "sin nombre", datos, ine_imagen.filename or "ine.jpg",
                         frase, umbral_rostro, max_intentos, apariencia)
    except HTTPException as exc:
        # Mismos errores que veria el proyecto al crear la sesion, en la pagina.
        return _pagina_inicio(request, str(exc.detail), frase, etiqueta)
    return RedirectResponse(f"/lab/resultado/{s.id}", status_code=303)


@router.post("/repetir/{sid}")
async def repetir(request: Request, sid: str):
    """Otra sesion con la misma INE y los mismos parametros (para que la persona lo intente de nuevo)."""
    c = _corridas.get(sid)
    if not c:
        raise HTTPException(status_code=404, detail="Esa INE ya no está en memoria: súbela de nuevo.")
    p = c["params"]
    s = await _crear(request, c["etiqueta"], c["ine"], c["nombre_archivo"], p["frase"], p["umbral"], p["max_intentos"],
                     p.get("apariencia"))
    return RedirectResponse(f"/lab/resultado/{s.id}", status_code=303)


@router.get("/resultado/{sid}", response_class=HTMLResponse)
async def resultado(request: Request, sid: str):
    s = sesiones._sesiones.get(sid)
    c = _corridas.get(sid)
    if s is None or c is None:
        # Tras reiniciar solo queda el JSON.
        guardado = next((f for f in _previas() if f["id"] == sid), None)
        if not guardado:
            raise HTTPException(status_code=404, detail="Prueba no encontrada.")
        with open(os.path.join(CARPETA, guardado["archivo"]), encoding="utf-8") as fh:
            d = json.load(fh)
        vista = {"id": sid, "etiqueta": d.get("etiqueta"), "estado": d.get("estado"), "aprobada": d.get("aprobada"),
                 "frase": d.get("frase"), "umbral_rostro": d.get("umbral_rostro"), "intentos_max": None,
                 "url_captura": None, "qr": None, "url_telefono": None, "diagnostico": d.get("ine"), "historial": d.get("intentos") or [],
                 "rostro_ine": False, "fotogramas": [], "en_memoria": False, "archivo": guardado["archivo"]}
    else:
        _guardar(s)
        fotos = [n.split("/")[1] for n in (s.evidencia or {}) if n.startswith("fotograma/")]
        vista = {"id": sid, "etiqueta": c["etiqueta"], "estado": s.estado, "aprobada": s.aprobada, "frase": s.frase,
                 "umbral_rostro": s.umbral_rostro, "intentos_max": s.max_intentos,
                 "url_captura": f"/captura/{s.token}" if s.estado == "pendiente" else None,
                 "qr": _qr(f"{s.url_base}/captura/{s.token}") if s.estado == "pendiente" else None,
                 "url_telefono": f"{s.url_base}/captura/{s.token}",
                 "diagnostico": c["diagnostico"], "historial": s.historial, "rostro_ine": bool(c["rostro_ine"]),
                 "fotogramas": fotos, "en_memoria": True, "archivo": os.path.basename(c["archivo"])}
    vista["intentos"] = [_intento(i) for i in reversed(vista["historial"])]
    return templates.TemplateResponse("lab_resultado.html", {
        "request": request, "p": vista, "umbrales": _umbrales(),
        "en_proceso": vista["estado"] in ("procesando", "entregando"),
        # Mientras la persona hace la prueba en su telefono, la pagina se actualiza sola.
        "activa": vista["en_memoria"] and vista["estado"] in (*sesiones.ACTIVOS, "entregando"),
    })


@router.get("/resultado/{sid}/rostro-ine.jpg")
async def rostro_ine(sid: str):
    c = _corridas.get(sid)
    if not c or not c["rostro_ine"]:
        raise HTTPException(status_code=404, detail="No disponible.")
    return Response(c["rostro_ine"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/resultado/{sid}/fotograma/{orden}.jpg")
async def fotograma(sid: str, orden: int):
    s = sesiones._sesiones.get(sid)
    a = (s.evidencia or {}).get(f"fotograma/{orden}") if s else None
    if not a or not os.path.exists(a["archivo"]):
        raise HTTPException(status_code=404, detail="No disponible.")
    with open(a["archivo"], "rb") as fh:
        return Response(fh.read(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})

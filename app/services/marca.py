"""Marca del proyecto en la pantalla de captura: logo y colores, opcionales.

Los colores llegan en hexadecimal y se validan con una expresion estricta:
asi se pueden escribir tal cual en el CSS de la pagina sin riesgo.
"""
import re
from io import BytesIO

from fastapi import HTTPException, UploadFile
from PIL import Image

from app.services.upload_io import read_upload_limited

_HEX = re.compile(r"#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})")
LOGO_MAX_MB = 1
LOGO_MAX_LADO = 2000
# SVG no: servido desde este dominio podria ejecutar scripts.
_LOGO_FORMATOS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def color(texto: str | None, campo: str) -> str | None:
    """'#1B56D6', '1b56d6' o '#18d' -> '#1b56d6'. Vacio -> None."""
    if not texto or not texto.strip():
        return None
    m = _HEX.fullmatch(texto.strip())
    if not m:
        raise HTTPException(status_code=422, detail=f"{campo} debe ser un color hexadecimal, ej. #1B56D6.")
    h = m.group(1).lower()
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def texto_sobre(fondo: str) -> str:
    """Color de texto legible sobre `fondo` (#rrggbb): casi negro o blanco, segun su luminancia."""
    canales = [int(fondo[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lineal = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in canales]
    luminancia = 0.2126 * lineal[0] + 0.7152 * lineal[1] + 0.0722 * lineal[2]
    return "#111111" if luminancia > 0.4 else "#ffffff"


async def logo(upload: UploadFile | None) -> tuple[bytes, str] | None:
    """(bytes, content-type) del logo, o None si no se mando."""
    if upload is None or not upload.filename:
        return None
    raw = await read_upload_limited(upload, LOGO_MAX_MB, "logo")
    if not raw:
        return None
    try:
        img = Image.open(BytesIO(raw))
        formato = img.format
        ancho, alto = img.size
    except Exception:
        raise HTTPException(status_code=422, detail="logo debe ser una imagen PNG, JPEG o WEBP.")
    if formato not in _LOGO_FORMATOS:
        raise HTTPException(status_code=422, detail="logo debe ser una imagen PNG, JPEG o WEBP.")
    if max(ancho, alto) > LOGO_MAX_LADO:
        raise HTTPException(status_code=422, detail=f"logo no debe pasar de {LOGO_MAX_LADO} px por lado.")
    return raw, _LOGO_FORMATOS[formato]

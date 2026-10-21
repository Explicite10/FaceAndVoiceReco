"""Expediente: guarda en disco la INE, el video y la foto de cada intento, y los sirve al panel.

Los archivos se guardan en binario, tal cual llegaron, en
    <EVIDENCIA_DIR>/<sesion>/<intento>/ine.jpg | video.webm | foto.jpg
En la base solo va la ruta relativa, el tipo, el tamaño y su SHA-256 (para
comprobar despues que no cambio). Nada se guarda en base64 ni dentro de la base.

No son publicos: solo se sirven por /panel/evidencia/<id> con sesion de admin.
"""
import hashlib
import logging
import re
import shutil
from pathlib import Path

import aiofiles
from fastapi import Request, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

_CHUNK = 256 * 1024
# Los ids de sesion son token_urlsafe: solo estos caracteres. Cualquier otra cosa no arma rutas.
_ID_SEGURO = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# Tipo de imagen por sus primeros bytes (no por el nombre que mando el navegador).
_IMAGENES = [(b"\xff\xd8\xff", ".jpg", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", ".png", "image/png")]
_VIDEOS = {".webm": "video/webm", ".mp4": "video/mp4", ".mov": "video/quicktime", ".m4v": "video/mp4",
           ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}


def base() -> Path:
    return Path(settings.evidencia_dir).resolve()


def _tipo_imagen(cabeza: bytes) -> tuple[str, str]:
    for firma, ext, mime in _IMAGENES:
        if cabeza.startswith(firma):
            return ext, mime
    if cabeza[:4] == b"RIFF" and cabeza[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".img", "application/octet-stream"


def _tipo_video(nombre: str | None) -> tuple[str, str]:
    ext = Path(nombre or "").suffix.lower()
    return (ext, _VIDEOS[ext]) if ext in _VIDEOS else (".bin", "application/octet-stream")


async def _copiar(upload: UploadFile, destino: Path) -> tuple[int, str]:
    """Copia el upload (ya leido por la verificacion) desde el inicio, por bloques. (bytes, sha256)."""
    await upload.seek(0)
    h, total = hashlib.sha256(), 0
    async with aiofiles.open(destino, "wb") as out:
        while chunk := await upload.read(_CHUNK):
            h.update(chunk)
            total += len(chunk)
            await out.write(chunk)
    return total, h.hexdigest()


async def guardar(sid: str, intento_id: int, archivos: dict[str, UploadFile]) -> list[dict]:
    """Guarda los archivos del intento. `archivos`: {"ine": ..., "video": ...} o {"ine": ..., "foto": ...}.

    Devuelve lo que va a la base: tipo, ruta relativa, mime, bytes y sha256.
    """
    if not _ID_SEGURO.match(sid):
        raise ValueError("id de sesion invalido")
    carpeta = base() / sid / str(int(intento_id))
    carpeta.mkdir(parents=True, exist_ok=True, mode=0o700)
    guardados = []
    try:
        for tipo, upload in archivos.items():
            await upload.seek(0)
            if tipo == "video":
                ext, mime = _tipo_video(upload.filename)
            else:
                ext, mime = _tipo_imagen(await upload.read(16))
            destino = carpeta / f"{tipo}{ext}"
            total, sha = await _copiar(upload, destino)
            guardados.append({"tipo": tipo, "ruta": destino.relative_to(base()).as_posix(),
                              "mime": mime, "bytes": total, "sha256": sha})
    except Exception:
        shutil.rmtree(carpeta, ignore_errors=True)  # nada a medias
        raise
    return guardados


def borrar_intento(sid: str, intento_id: int) -> None:
    if _ID_SEGURO.match(sid):
        shutil.rmtree(base() / sid / str(int(intento_id)), ignore_errors=True)


def borrar_sesion(sid: str) -> None:
    """Borra todos los archivos de una sesion (al borrar la sesion de la base)."""
    if _ID_SEGURO.match(sid):
        shutil.rmtree(base() / sid, ignore_errors=True)


def ruta_absoluta(relativa: str) -> Path | None:
    """La ruta en disco, solo si queda dentro de la carpeta del expediente."""
    p = (base() / relativa).resolve()
    return p if p.is_relative_to(base()) and p.is_file() else None


def _leer(path: Path, inicio: int, fin: int):
    with open(path, "rb") as f:
        f.seek(inicio)
        falta = fin - inicio + 1
        while falta > 0:
            chunk = f.read(min(_CHUNK, falta))
            if not chunk:
                break
            falta -= len(chunk)
            yield chunk


def servir(request: Request, path: Path, mime: str, nombre: str, descargar: bool = False) -> Response:
    """El archivo, con soporte de rangos (Range): el video se puede adelantar y Safari lo reproduce."""
    tam = path.stat().st_size
    encabezados = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": f'{"attachment" if descargar else "inline"}; filename="{nombre}"',
    }
    rango = request.headers.get("range", "")
    if rango.startswith("bytes="):
        a, _, b = rango[6:].split(",")[0].strip().partition("-")
        try:
            if a == "":  # "bytes=-500": los ultimos 500
                inicio, fin = max(0, tam - int(b)), tam - 1
            else:
                inicio, fin = int(a), (int(b) if b else tam - 1)
        except ValueError:
            inicio, fin = tam, tam
        if inicio >= tam or inicio > fin:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{tam}"})
        fin = min(fin, tam - 1)
        encabezados["Content-Range"] = f"bytes {inicio}-{fin}/{tam}"
        encabezados["Content-Length"] = str(fin - inicio + 1)
        return StreamingResponse(_leer(path, inicio, fin), status_code=206, media_type=mime, headers=encabezados)
    encabezados["Content-Length"] = str(tam)
    return StreamingResponse(_leer(path, 0, tam - 1), media_type=mime, headers=encabezados)

"""Proyectos autorizados a usar la prueba de vida (tabla `proyectos`).

Cada proyecto tiene su API key (solo se guarda su hash), el secreto con el
que se firman sus avisos y los dominios a los que puede regresar al usuario,
mandar avisos e incrustar la captura. Se administran con `python -m app.cli`.
"""
import hashlib
import hmac
import secrets
import uuid
from urllib.parse import urlparse

from fastapi import HTTPException

from app import db
from app.core.config import settings


def _hash(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def _nueva_api_key() -> str:
    return "pv_" + secrets.token_urlsafe(32)


def _publico(row) -> dict:
    p = dict(row)
    p.pop("api_key_hash", None)
    p.pop("webhook_secreto", None)
    return p


async def crear(nombre: str, webhook_url: str | None = None, dominios: list[str] | None = None) -> dict:
    """Registra un proyecto. La API key solo se devuelve aqui; en la base queda su hash."""
    api_key = _nueva_api_key()
    proyecto = {
        "id": "prj_" + uuid.uuid4().hex[:16],
        "nombre": nombre,
        "webhook_secreto": "whsec_" + secrets.token_urlsafe(32),
        "webhook_url": webhook_url,
        "dominios_permitidos": [d.lower().strip() for d in (dominios or []) if d.strip()],
    }
    if webhook_url:
        validar_url(webhook_url, proyecto, "webhook_url")
    await db.pool().execute(
        "INSERT INTO proyectos (id, nombre, api_key_hash, webhook_secreto, webhook_url, dominios_permitidos)"
        " VALUES ($1, $2, $3, $4, $5, $6)",
        proyecto["id"], nombre, _hash(api_key), proyecto["webhook_secreto"], webhook_url,
        proyecto["dominios_permitidos"],
    )
    return {**proyecto, "api_key": api_key}


async def listar() -> list[dict]:
    rows = await db.pool().fetch("SELECT * FROM proyectos ORDER BY creado")
    return [_publico(r) for r in rows]


async def _actualizar(proyecto_id: str, columna: str, valor) -> None:
    resultado = await db.pool().execute(f"UPDATE proyectos SET {columna} = $2 WHERE id = $1", proyecto_id, valor)
    if resultado.endswith(" 0"):
        raise KeyError(f"No existe el proyecto {proyecto_id}")


async def rotar_api_key(proyecto_id: str) -> str:
    api_key = _nueva_api_key()
    await _actualizar(proyecto_id, "api_key_hash", _hash(api_key))
    return api_key


async def desactivar(proyecto_id: str) -> None:
    await _actualizar(proyecto_id, "activo", False)


async def activar(proyecto_id: str) -> None:
    await _actualizar(proyecto_id, "activo", True)


async def actualizar_webhook(proyecto_id: str, webhook_url: str | None) -> None:
    proyecto = await por_id(proyecto_id)
    if proyecto is None:
        raise KeyError(f"No existe el proyecto {proyecto_id}")
    if webhook_url:
        validar_url(webhook_url, proyecto, "webhook_url")
    await _actualizar(proyecto_id, "webhook_url", webhook_url)


async def actualizar_dominios(proyecto_id: str, dominios: list[str]) -> None:
    await _actualizar(proyecto_id, "dominios_permitidos", [d.lower().strip() for d in dominios if d.strip()])


async def por_id(proyecto_id: str) -> dict | None:
    row = await db.pool().fetchrow("SELECT * FROM proyectos WHERE id = $1", proyecto_id)
    return dict(row) if row else None


async def por_api_key(api_key: str) -> dict | None:
    """Proyecto activo dueño de esa llave, o None."""
    row = await db.pool().fetchrow(
        "SELECT * FROM proyectos WHERE api_key_hash = $1 AND activo", _hash(api_key)
    )
    # El hash ya se busco por igualdad en la base; compare_digest evita depender de eso.
    if row and hmac.compare_digest(row["api_key_hash"], _hash(api_key)):
        return dict(row)
    return None


def validar_url(url: str, proyecto: dict, campo: str) -> None:
    """Evita redirecciones abiertas y avisos hacia dominios ajenos al proyecto."""
    p = urlparse(url)
    if p.scheme != "https" and not (p.scheme == "http" and settings.allow_http_urls):
        raise HTTPException(status_code=422, detail=f"{campo} debe usar https://")
    if not p.hostname:
        raise HTTPException(status_code=422, detail=f"{campo} no es una URL válida")
    dominios = proyecto["dominios_permitidos"]
    if dominios and not any(p.hostname == d or p.hostname.endswith("." + d) for d in dominios):
        raise HTTPException(status_code=422, detail=f"{campo} no pertenece a los dominios permitidos del proyecto")

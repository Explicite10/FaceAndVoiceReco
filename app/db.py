"""PostgreSQL: el token del QR y un resumen de cada sesion de la demo.

Aqui nunca entran el video, la INE, la cara, la voz ni la IP: solo cuando se
abrio cada sesion (alguien escaneo el QR), cuantas pruebas hizo y como salio
la ultima. Sirve para contar cuantas personas hicieron la prueba.

Sin DATABASE_URL todo queda en memoria (para desarrollo): se pierde al reiniciar.
Las tablas se crean al arrancar si no existen.
"""
import logging
import secrets
from datetime import datetime, timezone

import asyncpg

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ajustes (
    clave text PRIMARY KEY,
    valor text NOT NULL
);

CREATE TABLE IF NOT EXISTS sesiones (
    id              text PRIMARY KEY,
    creada          timestamptz NOT NULL DEFAULT now(),
    pruebas         integer NOT NULL DEFAULT 0,
    aprobadas       integer NOT NULL DEFAULT 0,
    ultima_prueba   timestamptz,
    ultimo_tipo     text,
    ultimo_aprobado boolean
);
CREATE INDEX IF NOT EXISTS sesiones_creada ON sesiones (creada DESC);
"""

# Respaldo en memoria cuando no hay base de datos.
_mem_ajustes: dict[str, str] = {}
_mem_sesiones: dict[str, dict] = {}


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


async def connect() -> None:
    global _pool
    if not settings.database_url:
        logger.warning("DATABASE_URL vacio: las sesiones de la demo se guardan solo en memoria.")
        return
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        await conn.execute(_SCHEMA)
    logger.info("Base de datos lista")


async def close() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def enabled() -> bool:
    return _pool is not None


# ---------------------------------------------------------------- QR

async def qr_token() -> str:
    """Token vigente del QR. Se crea la primera vez que se pide."""
    if _pool is None:
        return _mem_ajustes.setdefault("qr_token", secrets.token_urlsafe(32))
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ajustes (clave, valor) VALUES ('qr_token', $1) ON CONFLICT (clave) DO NOTHING",
            secrets.token_urlsafe(32),
        )
        return await conn.fetchval("SELECT valor FROM ajustes WHERE clave = 'qr_token'")


async def rotar_qr() -> str:
    """Token nuevo: el QR anterior y las sesiones abiertas con el dejan de servir."""
    nuevo = secrets.token_urlsafe(32)
    if _pool is None:
        _mem_ajustes["qr_token"] = nuevo
        return nuevo
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO ajustes (clave, valor) VALUES ('qr_token', $1) "
            "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor",
            nuevo,
        )
    return nuevo


# ---------------------------------------------------------------- sesiones

async def crear_sesion() -> str:
    sid = secrets.token_urlsafe(16)
    if _pool is None:
        _mem_sesiones[sid] = {"id": sid, "creada": _ahora(), "pruebas": 0, "aprobadas": 0,
                              "ultima_prueba": None, "ultimo_tipo": None, "ultimo_aprobado": None}
        return sid
    async with _pool.acquire() as conn:
        await conn.execute("INSERT INTO sesiones (id) VALUES ($1)", sid)
    return sid


async def registrar_prueba(sid: str, tipo: str, aprobada: bool) -> None:
    """Suma una prueba terminada a la sesion. `tipo`: "video" o "foto"."""
    if _pool is None:
        s = _mem_sesiones.get(sid)
        if s:
            s["pruebas"] += 1
            s["aprobadas"] += int(aprobada)
            s.update(ultima_prueba=_ahora(), ultimo_tipo=tipo, ultimo_aprobado=aprobada)
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "UPDATE sesiones SET pruebas = pruebas + 1, aprobadas = aprobadas + $2::int, "
            "ultima_prueba = now(), ultimo_tipo = $3, ultimo_aprobado = $4 WHERE id = $1",
            sid, int(aprobada), tipo, aprobada,
        )


def _fila(s) -> dict:
    return {
        "creada": s["creada"].isoformat(),
        "pruebas": s["pruebas"],
        "aprobadas": s["aprobadas"],
        "ultimo_tipo": s["ultimo_tipo"],
        "ultimo_aprobado": s["ultimo_aprobado"],
    }


async def resumen(ultimas: int = 15) -> dict:
    """Conteos para el panel y las sesiones mas recientes (sin su id)."""
    if _pool is None:
        todas = list(_mem_sesiones.values())
        recientes = sorted(todas, key=lambda s: s["creada"], reverse=True)[:ultimas]
        con_prueba = [s for s in todas if s["pruebas"] > 0]
        return {
            "sesiones": len(todas),
            "personas": len(con_prueba),
            "pruebas": sum(s["pruebas"] for s in todas),
            "aprobadas": sum(1 for s in con_prueba if s["ultimo_aprobado"]),
            "rechazadas": sum(1 for s in con_prueba if s["ultimo_aprobado"] is False),
            "recientes": [_fila(s) for s in recientes],
        }
    async with _pool.acquire() as conn:
        t = await conn.fetchrow(
            "SELECT count(*) AS sesiones, count(*) FILTER (WHERE pruebas > 0) AS personas, "
            "coalesce(sum(pruebas), 0) AS pruebas, "
            "count(*) FILTER (WHERE ultimo_aprobado) AS aprobadas, "
            "count(*) FILTER (WHERE ultimo_aprobado = false) AS rechazadas FROM sesiones"
        )
        filas = await conn.fetch("SELECT * FROM sesiones ORDER BY creada DESC LIMIT $1", ultimas)
    return {**{k: int(t[k]) for k in ("sesiones", "personas", "pruebas", "aprobadas", "rechazadas")},
            "recientes": [_fila(s) for s in filas]}

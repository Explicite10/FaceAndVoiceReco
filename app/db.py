"""PostgreSQL: proyectos autorizados y un resumen de cada sesion.

Aqui nunca entran el video, la INE, la cara ni la voz: solo quien pidio la
sesion, cuando, como termino y si se le entrego el resultado al proyecto.
Las tablas se crean al arrancar si no existen.
"""
import json
import logging

import asyncpg

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proyectos (
    id                  text PRIMARY KEY,
    nombre              text NOT NULL,
    api_key_hash        text NOT NULL UNIQUE,
    webhook_secreto     text NOT NULL,
    webhook_url         text,
    dominios_permitidos text[] NOT NULL DEFAULT '{}',
    activo              boolean NOT NULL DEFAULT true,
    creado              timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sesiones (
    id                 text PRIMARY KEY,
    proyecto_id        text NOT NULL REFERENCES proyectos(id),
    referencia_externa text,
    estado             text NOT NULL,
    aprobada           boolean,
    intentos           integer NOT NULL DEFAULT 0,
    max_intentos       integer NOT NULL,
    checks_fallidos    text[] NOT NULL DEFAULT '{}',
    url_retorno        text,
    webhook_url        text NOT NULL,
    metadata           jsonb NOT NULL DEFAULT '{}',
    entrega            jsonb,
    creada             timestamptz NOT NULL,
    expira             timestamptz NOT NULL,
    finalizada         timestamptz
);
CREATE INDEX IF NOT EXISTS sesiones_proyecto_creada ON sesiones (proyecto_id, creada DESC);
CREATE INDEX IF NOT EXISTS sesiones_estado ON sesiones (estado);

CREATE TABLE IF NOT EXISTS intentos (
    sesion_id          text NOT NULL REFERENCES sesiones(id),
    numero             integer NOT NULL,
    aprobado           boolean NOT NULL,
    checks_fallidos    text[] NOT NULL DEFAULT '{}',
    duracion_ms        integer,
    creado             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sesion_id, numero)
);
"""


async def _init_connection(conn: asyncpg.Connection) -> None:
    # jsonb entra y sale como dict de Python.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect() -> None:
    global _pool
    if not settings.database_url:
        logger.warning("DATABASE_URL vacio: las sesiones para proyectos estan desactivadas.")
        return
    _pool = await asyncpg.create_pool(settings.database_url, min_size=1, max_size=5, init=_init_connection)
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


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("La base de datos no esta configurada (DATABASE_URL).")
    return _pool

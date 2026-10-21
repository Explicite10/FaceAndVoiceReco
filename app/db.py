"""PostgreSQL: el token del QR y un resumen de cada sesion de la demo.

Aqui nunca entran el video, la INE, la cara, la voz ni la IP: solo cuando se
abrio cada sesion (alguien escaneo el QR), si inicio la prueba, y por cada
intento cuando fue, si se aprobo y los codigos de lo que fallo. Sirve para
contar cuantas personas hicieron la prueba y en que paso se quedaron.

Sin DATABASE_URL todo queda en memoria (para desarrollo): se pierde al reiniciar.
Las tablas y columnas se crean al arrancar si no existen.
"""
import json
import logging
import secrets
from collections import Counter
from datetime import datetime, timedelta, timezone

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
-- Retos pedidos: la persona activo la camara para grabar.
ALTER TABLE sesiones ADD COLUMN IF NOT EXISTS retos integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS intentos (
    id          bigserial PRIMARY KEY,
    sesion_id   text NOT NULL REFERENCES sesiones(id) ON DELETE CASCADE,
    creado      timestamptz NOT NULL DEFAULT now(),
    tipo        text NOT NULL,
    aprobado    boolean NOT NULL,
    fallos      text[] NOT NULL DEFAULT '{}',
    duracion_ms integer
);
CREATE INDEX IF NOT EXISTS intentos_creado ON intentos (creado);
CREATE INDEX IF NOT EXISTS intentos_sesion ON intentos (sesion_id, creado);
-- Lo que vio la persona al terminar (checks y porcentajes), para "Ver registro" del panel.
ALTER TABLE intentos ADD COLUMN IF NOT EXISTS detalle jsonb;
"""

# Bloques de la grafica de actividad.
BLOQUE = timedelta(minutes=15)

# Respaldo en memoria cuando no hay base de datos.
_mem_ajustes: dict[str, str] = {}
_mem_sesiones: dict[str, dict] = {}
_mem_intentos: list[dict] = []


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
        _mem_sesiones[sid] = {"id": sid, "creada": _ahora(), "pruebas": 0, "aprobadas": 0, "retos": 0,
                              "ultima_prueba": None, "ultimo_tipo": None, "ultimo_aprobado": None}
        return sid
    async with _pool.acquire() as conn:
        await conn.execute("INSERT INTO sesiones (id) VALUES ($1)", sid)
    return sid


async def registrar_reto(sid: str) -> None:
    """La persona pidio un reto: activo la camara para grabar la prueba de vida."""
    if _pool is None:
        if sid in _mem_sesiones:
            _mem_sesiones[sid]["retos"] += 1
        return
    async with _pool.acquire() as conn:
        await conn.execute("UPDATE sesiones SET retos = retos + 1 WHERE id = $1", sid)


async def registrar_prueba(sid: str, tipo: str, aprobada: bool,
                           fallos: list[str] | None = None, duracion_ms: int | None = None,
                           detalle: dict | None = None) -> None:
    """Suma una prueba terminada. `tipo`: "video" o "foto"; `fallos`: codigos de los checks que
    fallaron; `detalle`: el resultado que vio la persona (checks y porcentajes), para el panel."""
    fallos = list(fallos or [])
    if _pool is None:
        s = _mem_sesiones.get(sid)
        if s:
            s["pruebas"] += 1
            s["aprobadas"] += int(aprobada)
            s.update(ultima_prueba=_ahora(), ultimo_tipo=tipo, ultimo_aprobado=aprobada)
            _mem_intentos.append({"sesion_id": sid, "creado": _ahora(), "tipo": tipo, "aprobado": aprobada,
                                  "fallos": fallos, "duracion_ms": duracion_ms, "detalle": detalle})
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            n = await conn.execute(
                "UPDATE sesiones SET pruebas = pruebas + 1, aprobadas = aprobadas + $2::int, "
                "ultima_prueba = now(), ultimo_tipo = $3, ultimo_aprobado = $4 WHERE id = $1",
                sid, int(aprobada), tipo, aprobada,
            )
            if n.endswith(" 1"):
                await conn.execute(
                    "INSERT INTO intentos (sesion_id, tipo, aprobado, fallos, duracion_ms, detalle) "
                    "VALUES ($1, $2, $3, $4, $5, $6::jsonb)",
                    sid, tipo, aprobada, fallos, duracion_ms, json.dumps(detalle) if detalle is not None else None,
                )


# ---------------------------------------------------------------- resumen para el panel

def _etapa(s) -> str:
    """Hasta donde llego la sesion."""
    if s["pruebas"] > 0:
        return "aprobada" if s["ultimo_aprobado"] else "no_aprobada"
    return "inicio" if s["retos"] > 0 else "escaneo"


def _fila(s) -> dict:
    return {
        # Solo lo ve el admin (para "Ver registro"); no sirve para entrar: la cookie va firmada.
        "id": s["id"],
        "creada": s["creada"].isoformat(),
        "pruebas": s["pruebas"],
        "ultimo_tipo": s["ultimo_tipo"],
        "ultimo_aprobado": s["ultimo_aprobado"],
        "etapa": _etapa(s),
    }


def _bloque(t: datetime) -> datetime:
    t = t.astimezone(timezone.utc)
    minutos = int(BLOQUE.total_seconds() // 60)
    return t.replace(minute=t.minute - t.minute % minutos, second=0, microsecond=0)


def _actividad(escaneos: list[datetime], intentos: list[datetime]) -> list[dict]:
    """Escaneos y pruebas por bloque de 15 minutos, sin huecos entre el primero y el ultimo."""
    if not escaneos and not intentos:
        return []
    e, p = Counter(map(_bloque, escaneos)), Counter(map(_bloque, intentos))
    inicio, fin = min([*e, *p]), max([*e, *p])
    out, t = [], inicio
    while t <= fin and len(out) < 500:
        out.append({"t": t.isoformat(), "escaneos": e.get(t, 0), "pruebas": p.get(t, 0)})
        t += BLOQUE
    return out


def _momentos_de_pruebas(sesiones: list, intentos: list) -> list[datetime]:
    """Cuando ocurrio cada prueba, para la grafica de actividad.

    Las pruebas de antes de que existiera la tabla `intentos` solo tienen la
    hora de la ultima: se cuentan ahi para que no desaparezcan de la grafica.
    """
    por_sesion = Counter(i["sesion_id"] for i in intentos)
    momentos = [i["creado"] for i in intentos]
    for s in sesiones:
        previas = s["pruebas"] - por_sesion.get(s["id"], 0)
        if previas > 0 and s["ultima_prueba"] is not None:
            momentos += [s["ultima_prueba"]] * previas
    return momentos


def _armar(sesiones: list, intentos: list, ultimas: int) -> dict:
    con_prueba = [s for s in sesiones if s["pruebas"] > 0]
    iniciaron = [s for s in sesiones if s["retos"] > 0 or s["pruebas"] > 0]
    aprobadas = sum(1 for s in con_prueba if s["ultimo_aprobado"])
    rechazadas = sum(1 for s in con_prueba if s["ultimo_aprobado"] is False)
    # Motivos: de los intentos no aprobados, cuantas veces fallo cada check.
    motivos = Counter(f for i in intentos if not i["aprobado"] for f in set(i["fallos"]))
    recientes = sorted(sesiones, key=lambda s: s["creada"], reverse=True)[:ultimas]
    return {
        "sesiones": len(sesiones),
        "personas": len(con_prueba),
        "pruebas": sum(s["pruebas"] for s in sesiones),
        "aprobadas": aprobadas,
        "rechazadas": rechazadas,
        "embudo": {
            "escanearon": len(sesiones),
            "iniciaron": len(iniciaron),
            "verificaron": len(con_prueba),
            "aprobadas": aprobadas,
        },
        "resultado": {
            "aprobadas": aprobadas,
            "no_aprobadas": rechazadas,
            "abandonaron": len(sesiones) - len(con_prueba),
        },
        "motivos": [{"codigo": c, "n": n} for c, n in motivos.most_common(8)],
        "actividad": _actividad([s["creada"] for s in sesiones], _momentos_de_pruebas(sesiones, intentos)),
        # Desde cuando hay detalle por intento (motivos); antes solo se guardaba el resumen.
        "motivos_desde": min((i["creado"] for i in intentos), default=_ahora()).isoformat(),
        # Hay pruebas sin detalle (de antes de la tabla intentos): sus motivos no se conocen.
        "motivos_parciales": sum(s["pruebas"] for s in sesiones) > len(intentos),
        "recientes": [_fila(s) for s in recientes],
    }


async def resumen(ultimas: int = 15) -> dict:
    """Conteos, embudo, motivos y actividad para el panel (sin ids ni datos personales)."""
    if _pool is None:
        return _armar(list(_mem_sesiones.values()), list(_mem_intentos), ultimas)
    async with _pool.acquire() as conn:
        sesiones = await conn.fetch(
            "SELECT id, creada, pruebas, retos, ultima_prueba, ultimo_tipo, ultimo_aprobado FROM sesiones")
        intentos = await conn.fetch("SELECT sesion_id, creado, aprobado, fallos FROM intentos")
    return _armar([dict(s) for s in sesiones], [dict(i) for i in intentos], ultimas)


def _intento(i) -> dict:
    detalle = i["detalle"]
    if isinstance(detalle, str):  # asyncpg entrega jsonb como texto
        detalle = json.loads(detalle)
    return {
        "creado": i["creado"].isoformat(),
        "tipo": i["tipo"],
        "aprobado": i["aprobado"],
        "fallos": list(i["fallos"] or []),
        "duracion_ms": i["duracion_ms"],
        "detalle": detalle,
    }


async def detalle_sesion(sid: str) -> dict | None:
    """Una sesion con todos sus intentos (el mas reciente primero), para "Ver registro"."""
    if _pool is None:
        s = _mem_sesiones.get(sid)
        if s is None:
            return None
        # Empates de hora (mismo milisegundo): gana el que se registro despues.
        propios = [(n, i) for n, i in enumerate(_mem_intentos) if i["sesion_id"] == sid]
        intentos = [i for _, i in sorted(propios, key=lambda x: (x[1]["creado"], x[0]), reverse=True)]
    else:
        async with _pool.acquire() as conn:
            s = await conn.fetchrow(
                "SELECT id, creada, pruebas, retos, ultima_prueba, ultimo_tipo, ultimo_aprobado FROM sesiones WHERE id = $1",
                sid)
            if s is None:
                return None
            intentos = await conn.fetch(
                "SELECT creado, tipo, aprobado, fallos, duracion_ms, detalle FROM intentos "
                "WHERE sesion_id = $1 ORDER BY creado DESC, id DESC", sid)
    return {
        "sesion": {
            **_fila(s),
            "retos": s["retos"],
            "ultima_prueba": s["ultima_prueba"].isoformat() if s["ultima_prueba"] else None,
        },
        "intentos": [_intento(i) for i in intentos],
        # Pruebas de antes de que se guardara el detalle por intento.
        "sin_detalle": max(0, s["pruebas"] - len(intentos)),
    }

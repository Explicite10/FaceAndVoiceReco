"""PostgreSQL: el token del QR, un resumen de cada sesion de la demo y su expediente.

Por cada sesion (alguien escaneo el QR): si inicio la prueba y, por cada intento,
cuando fue, si se aprobo, los codigos de lo que fallo y el resultado que vio.
El expediente (INE, video, foto) se guarda en disco, en binario: aqui solo van
sus rutas relativas, tipo, tamaño y SHA-256 (app.services.expediente). Nunca
entran archivos ni base64 a la base, ni la IP de nadie.

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

-- Expediente: archivos del intento guardados en disco. Solo la ruta, nunca el archivo.
CREATE TABLE IF NOT EXISTS evidencias (
    id         bigserial PRIMARY KEY,
    intento_id bigint NOT NULL REFERENCES intentos(id) ON DELETE CASCADE,
    tipo       text NOT NULL,          -- ine | video | foto
    ruta       text NOT NULL,          -- relativa a EVIDENCIA_DIR
    mime       text NOT NULL,
    bytes      bigint NOT NULL,
    sha256     text NOT NULL,
    creado     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS evidencias_intento ON evidencias (intento_id);
"""

# Bloques de la grafica de actividad.
BLOQUE = timedelta(minutes=15)

# Respaldo en memoria cuando no hay base de datos.
_mem_ajustes: dict[str, str] = {}
_mem_sesiones: dict[str, dict] = {}
_mem_intentos: list[dict] = []
_mem_evidencias: list[dict] = []


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

def _sesion_vacia(sid: str) -> dict:
    return {"id": sid, "creada": _ahora(), "pruebas": 0, "aprobadas": 0, "retos": 0,
            "ultima_prueba": None, "ultimo_tipo": None, "ultimo_aprobado": None}


def nuevo_id_sesion() -> str:
    """Id para la cookie de quien abre el QR; el registro se crea despues (asegurar_sesion)."""
    return secrets.token_urlsafe(16)


async def crear_sesion() -> str:
    sid = nuevo_id_sesion()
    if _pool is None:
        _mem_sesiones[sid] = _sesion_vacia(sid)
        return sid
    async with _pool.acquire() as conn:
        await conn.execute("INSERT INTO sesiones (id) VALUES ($1)", sid)
    return sid


# Si se borraron los registros (p. ej. para empezar en cero) mientras alguien tenia la
# cookie vigente, su sesion se vuelve a crear al usarla: nadie se queda sin contarse.
_ASEGURAR = "INSERT INTO sesiones (id) VALUES ($1) ON CONFLICT (id) DO NOTHING"


async def asegurar_sesion(sid: str) -> None:
    """Crea la sesion si ya no existe en la base (la cookie sigue siendo valida)."""
    if _pool is None:
        _mem_sesiones.setdefault(sid, _sesion_vacia(sid))
        return
    async with _pool.acquire() as conn:
        await conn.execute(_ASEGURAR, sid)


async def registrar_reto(sid: str) -> None:
    """La persona pidio un reto: activo la camara para grabar la prueba de vida."""
    if _pool is None:
        _mem_sesiones.setdefault(sid, _sesion_vacia(sid))["retos"] += 1
        return
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_ASEGURAR, sid)
            await conn.execute("UPDATE sesiones SET retos = retos + 1 WHERE id = $1", sid)


async def registrar_prueba(sid: str, tipo: str, aprobada: bool,
                           fallos: list[str] | None = None, duracion_ms: int | None = None,
                           detalle: dict | None = None) -> int | None:
    """Suma una prueba terminada. `tipo`: "video" o "foto"; `fallos`: codigos de los checks que
    fallaron; `detalle`: el resultado que vio la persona (checks y porcentajes), para el panel.
    Devuelve el id del intento. Si la sesion se habia borrado, se vuelve a crear."""
    fallos = list(fallos or [])
    if _pool is None:
        s = _mem_sesiones.setdefault(sid, _sesion_vacia(sid))
        s["pruebas"] += 1
        s["aprobadas"] += int(aprobada)
        s.update(ultima_prueba=_ahora(), ultimo_tipo=tipo, ultimo_aprobado=aprobada)
        iid = max((i["id"] for i in _mem_intentos), default=0) + 1  # unico aunque se borren intentos
        _mem_intentos.append({"id": iid, "sesion_id": sid, "creado": _ahora(), "tipo": tipo, "aprobado": aprobada,
                              "fallos": fallos, "duracion_ms": duracion_ms, "detalle": detalle})
        return iid
    async with _pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(_ASEGURAR, sid)
            n = await conn.execute(
                "UPDATE sesiones SET pruebas = pruebas + 1, aprobadas = aprobadas + $2::int, "
                "ultima_prueba = now(), ultimo_tipo = $3, ultimo_aprobado = $4 WHERE id = $1",
                sid, int(aprobada), tipo, aprobada,
            )
            if not n.endswith(" 1"):
                return None
            return await conn.fetchval(
                "INSERT INTO intentos (sesion_id, tipo, aprobado, fallos, duracion_ms, detalle) "
                "VALUES ($1, $2, $3, $4, $5, $6::jsonb) RETURNING id",
                sid, tipo, aprobada, fallos, duracion_ms, json.dumps(detalle) if detalle is not None else None,
            )


async def registrar_evidencias(intento_id: int, archivos: list[dict]) -> None:
    """Las rutas de los archivos guardados del intento (tipo, ruta, mime, bytes, sha256)."""
    if _pool is None:
        for a in archivos:
            _mem_evidencias.append({"id": len(_mem_evidencias) + 1, "intento_id": intento_id, **a})
        return
    async with _pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO evidencias (intento_id, tipo, ruta, mime, bytes, sha256) VALUES ($1, $2, $3, $4, $5, $6)",
            [(intento_id, a["tipo"], a["ruta"], a["mime"], a["bytes"], a["sha256"]) for a in archivos],
        )


async def obtener_evidencia(evid_id: int) -> dict | None:
    """Un archivo del expediente: ruta, mime, tipo y cuando se tomo (para servirlo al panel)."""
    if _pool is None:
        e = next((e for e in _mem_evidencias if e["id"] == evid_id), None)
        if e is None:
            return None
        i = next(i for i in _mem_intentos if i["id"] == e["intento_id"])
        return {**e, "creado": i["creado"]}
    async with _pool.acquire() as conn:
        e = await conn.fetchrow(
            "SELECT e.id, e.tipo, e.ruta, e.mime, e.bytes, e.sha256, i.creado FROM evidencias e "
            "JOIN intentos i ON i.id = e.intento_id WHERE e.id = $1", evid_id)
    return dict(e) if e else None


async def borrar_sesion(sid: str) -> bool:
    """Borra la sesion, sus intentos, las rutas de su expediente y los archivos en disco."""
    from app.services import expediente  # aqui para no importar FastAPI al cargar la base
    if _pool is None:
        if _mem_sesiones.pop(sid, None) is None:
            return False
        ids = {i["id"] for i in _mem_intentos if i["sesion_id"] == sid}
        _mem_intentos[:] = [i for i in _mem_intentos if i["sesion_id"] != sid]
        _mem_evidencias[:] = [e for e in _mem_evidencias if e["intento_id"] not in ids]
    else:
        async with _pool.acquire() as conn:
            if await conn.execute("DELETE FROM sesiones WHERE id = $1", sid) == "DELETE 0":
                return False
    expediente.borrar_sesion(sid)
    return True


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


def _armar(sesiones: list, intentos: list) -> dict:
    con_prueba = [s for s in sesiones if s["pruebas"] > 0]
    iniciaron = [s for s in sesiones if s["retos"] > 0 or s["pruebas"] > 0]
    aprobadas = sum(1 for s in con_prueba if s["ultimo_aprobado"])
    rechazadas = sum(1 for s in con_prueba if s["ultimo_aprobado"] is False)
    # Motivos: de los intentos no aprobados, cuantas veces fallo cada check.
    motivos = Counter(f for i in intentos if not i["aprobado"] for f in set(i["fallos"]))
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
    }


async def resumen() -> dict:
    """Conteos, embudo, motivos y actividad para el panel (sin ids ni datos personales)."""
    if _pool is None:
        return _armar(list(_mem_sesiones.values()), list(_mem_intentos))
    async with _pool.acquire() as conn:
        sesiones = await conn.fetch(
            "SELECT id, creada, pruebas, retos, ultima_prueba, ultimo_tipo, ultimo_aprobado FROM sesiones")
        intentos = await conn.fetch("SELECT sesion_id, creado, aprobado, fallos FROM intentos")
    return _armar([dict(s) for s in sesiones], [dict(i) for i in intentos])


POR_PAGINA = 10


async def sesiones_pagina(pagina: int = 1, por_pagina: int = POR_PAGINA) -> dict:
    """Una pagina de la tabla del panel: las sesiones mas recientes primero.

    Una pagina fuera de rango se ajusta a la primera o la ultima (p. ej. si se
    borraron sesiones mientras alguien veia la ultima pagina).
    """
    if _pool is None:
        todas = sorted(_mem_sesiones.values(), key=lambda s: (s["creada"], s["id"]), reverse=True)
        total = len(todas)
    else:
        async with _pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM sesiones")
    paginas = max(1, -(-total // por_pagina))
    pagina = min(max(1, pagina), paginas)
    desde = (pagina - 1) * por_pagina
    if _pool is None:
        filas = todas[desde:desde + por_pagina]
    else:
        async with _pool.acquire() as conn:
            filas = await conn.fetch(
                "SELECT id, creada, pruebas, retos, ultimo_tipo, ultimo_aprobado FROM sesiones "
                "ORDER BY creada DESC, id DESC LIMIT $1 OFFSET $2", por_pagina, desde)
    return {"total": total, "pagina": pagina, "paginas": paginas, "por_pagina": por_pagina,
            "filas": [_fila(s) for s in filas]}


def _intento(i, evidencias: list) -> dict:
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
        # Archivos del expediente: el panel los pide por id; la ruta en disco nunca sale.
        "expediente": [{"id": e["id"], "tipo": e["tipo"], "mime": e["mime"], "bytes": e["bytes"]}
                       for e in evidencias if e["intento_id"] == i["id"]],
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
        ids = {i["id"] for i in intentos}
        evidencias = [e for e in _mem_evidencias if e["intento_id"] in ids]
    else:
        async with _pool.acquire() as conn:
            s = await conn.fetchrow(
                "SELECT id, creada, pruebas, retos, ultima_prueba, ultimo_tipo, ultimo_aprobado FROM sesiones WHERE id = $1",
                sid)
            if s is None:
                return None
            intentos = await conn.fetch(
                "SELECT id, creado, tipo, aprobado, fallos, duracion_ms, detalle FROM intentos "
                "WHERE sesion_id = $1 ORDER BY creado DESC, id DESC", sid)
            evidencias = await conn.fetch(
                "SELECT e.id, e.intento_id, e.tipo, e.mime, e.bytes FROM evidencias e "
                "JOIN intentos i ON i.id = e.intento_id WHERE i.sesion_id = $1 ORDER BY e.id", sid)
    return {
        "sesion": {
            **_fila(s),
            "retos": s["retos"],
            "ultima_prueba": s["ultima_prueba"].isoformat() if s["ultima_prueba"] else None,
        },
        "intentos": [_intento(i, evidencias) for i in intentos],
        # Pruebas de antes de que se guardara el detalle por intento.
        "sin_detalle": max(0, s["pruebas"] - len(intentos)),
    }

"""Consultas del panel administrativo: totales, graficas y tabla de sesiones.

Todo sale de las tablas `sesiones`, `intentos` y `proyectos`: el resumen que ya
se guarda de cada sesion, sin biometria. Los filtros (proyecto y periodo) se
aplican igual a los totales, las graficas y la tabla.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app import db
from app.core.config import settings

# Periodos que ofrece el panel, en dias; 0 = todo.
PERIODOS = (1, 7, 30, 90, 0)
POR_PAGINA = 10
RESULTADOS = ("aprobada", "no_aprobada", "en_curso", "expirada", "cancelada", "entrega_fallida")

# Resultado de una sesion en una sola palabra. aprobada se llena al decidir,
# aunque la entrega al proyecto haya fallado despues.
_RESULTADO = """CASE WHEN s.aprobada THEN 'aprobada'
                     WHEN s.aprobada = false THEN 'no_aprobada'
                     WHEN s.estado IN ('expirada', 'cancelada') THEN s.estado
                     ELSE 'en_curso' END"""
_ENTREGA_FALLIDA = "(s.estado = 'error_entrega' OR s.entrega->>'ok' = 'false')"


def _zona() -> ZoneInfo:
    try:
        return ZoneInfo(settings.panel_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def periodo_valido(dias: int) -> int:
    return dias if dias in PERIODOS else 30


def _filtros(proyecto: str | None, dias: int, resultado: str | None = None,
             buscar: str | None = None) -> tuple[str, list]:
    """WHERE sobre la tabla sesiones (alias s) y sus parametros."""
    condiciones, params = [], []
    if proyecto:
        params.append(proyecto)
        condiciones.append(f"s.proyecto_id = ${len(params)}")
    if dias:
        params.append(datetime.now(timezone.utc) - timedelta(days=dias))
        condiciones.append(f"s.creada >= ${len(params)}")
    if resultado == "entrega_fallida":
        condiciones.append(_ENTREGA_FALLIDA)
    elif resultado in RESULTADOS:
        params.append(resultado)
        condiciones.append(f"{_RESULTADO} = ${len(params)}")
    if buscar:
        # Busqueda literal: los comodines de LIKE que escriba el usuario no cuentan.
        texto = buscar.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{texto}%")
        condiciones.append(f"(s.id ILIKE ${len(params)} OR s.referencia_externa ILIKE ${len(params)})")
    return ("WHERE " + " AND ".join(condiciones)) if condiciones else "", params


def _iso(fecha: datetime | None) -> str | None:
    return fecha.isoformat() if fecha else None


async def proyectos() -> list[dict]:
    rows = await db.pool().fetch("SELECT id, nombre, activo FROM proyectos ORDER BY nombre")
    return [dict(r) for r in rows]


async def resumen(proyecto: str | None, dias: int) -> dict:
    """Totales y datos de todas las graficas, con los mismos filtros."""
    where, params = _filtros(proyecto, dias)
    pool = db.pool()
    t = await pool.fetchrow(
        f"""
        SELECT count(*) AS sesiones,
               count(*) FILTER (WHERE s.intentos > 0) AS con_intento,
               count(*) FILTER (WHERE s.aprobada IS NOT NULL) AS con_resultado,
               count(*) FILTER (WHERE s.aprobada) AS aprobadas,
               count(*) FILTER (WHERE s.aprobada = false) AS no_aprobadas,
               count(*) FILTER (WHERE {_RESULTADO} = 'expirada') AS expiradas,
               count(*) FILTER (WHERE {_RESULTADO} = 'cancelada') AS canceladas,
               count(*) FILTER (WHERE {_RESULTADO} = 'en_curso') AS en_curso,
               count(*) FILTER (WHERE {_ENTREGA_FALLIDA}) AS entregas_fallidas
        FROM sesiones s {where}
        """,
        *params,
    )
    i = await pool.fetchrow(
        f"""
        SELECT count(*) AS intentos,
               count(*) FILTER (WHERE i.aprobado) AS aprobados,
               round(avg(i.duracion_ms))::int AS duracion_ms
        FROM intentos i JOIN sesiones s ON s.id = i.sesion_id {where}
        """,
        *params,
    )
    motivos = await pool.fetch(
        f"""
        SELECT c AS codigo, count(*) AS n
        FROM intentos i JOIN sesiones s ON s.id = i.sesion_id, unnest(i.checks_fallidos) AS c
        {where} {"AND" if where else "WHERE"} NOT i.aprobado
        GROUP BY c ORDER BY n DESC, c LIMIT 10
        """,
        *params,
    )
    por_proyecto = await pool.fetch(
        f"""
        SELECT p.nombre,
               count(*) FILTER (WHERE s.aprobada) AS aprobadas,
               count(*) FILTER (WHERE s.aprobada = false) AS no_aprobadas,
               count(*) FILTER (WHERE s.aprobada IS NULL) AS sin_resultado
        FROM sesiones s JOIN proyectos p ON p.id = s.proyecto_id {where}
        GROUP BY p.id, p.nombre ORDER BY count(*) DESC, p.nombre LIMIT 12
        """,
        *params,
    )
    return {
        "sesiones": t["sesiones"],
        "intentos": i["intentos"],
        "intentos_aprobados": i["aprobados"],
        "duracion_ms": i["duracion_ms"],
        "embudo": {k: t[k] for k in ("sesiones", "con_intento", "con_resultado", "aprobadas")},
        "resultado": {k: t[k] for k in ("aprobadas", "no_aprobadas", "en_curso", "expiradas", "canceladas")},
        "entregas_fallidas": t["entregas_fallidas"],
        "motivos": [dict(r) for r in motivos],
        "por_proyecto": [dict(r) for r in por_proyecto],
        "actividad": await _actividad(where, params, dias),
    }


async def _actividad(where: str, params: list, dias: int) -> dict:
    """Sesiones, intentos y aprobadas por hora (periodo de 1 dia) o por dia.

    La base agrupa por hora en UTC y aqui se pasa a la hora local: asi no
    depende de que PostgreSQL tenga la zona horaria instalada.
    """
    pool = db.pool()
    sesiones = await pool.fetch(
        f"""SELECT date_trunc('hour', s.creada) AS h, count(*) AS n, count(*) FILTER (WHERE s.aprobada) AS ok
            FROM sesiones s {where} GROUP BY 1""",
        *params,
    )
    intentos = await pool.fetch(
        f"""SELECT date_trunc('hour', i.creado) AS h, count(*) AS n
            FROM intentos i JOIN sesiones s ON s.id = i.sesion_id {where} GROUP BY 1""",
        *params,
    )
    zona = _zona()
    por_hora = dias == 1

    def bloque(h: datetime) -> datetime:
        local = h.astimezone(zona)
        return local.replace(minute=0, second=0, microsecond=0) if por_hora else \
            local.replace(hour=0, minute=0, second=0, microsecond=0)

    conteo: dict[datetime, list[int]] = {}
    for r in sesiones:
        c = conteo.setdefault(bloque(r["h"]), [0, 0, 0])
        c[0] += r["n"]
        c[2] += r["ok"]
    for r in intentos:
        conteo.setdefault(bloque(r["h"]), [0, 0, 0])[1] += r["n"]

    # Los bloques sin movimiento van en cero: sin ellos la linea une dias lejanos.
    ahora = bloque(datetime.now(timezone.utc))
    if dias:
        inicio = bloque(datetime.now(timezone.utc) - timedelta(days=dias))
    else:
        inicio = min(conteo, default=ahora)
    # Un dia de mas una hora: si hay cambio de horario (dias de 23 o 25 h), al
    # redondear se cae siempre en el dia siguiente.
    paso = timedelta(hours=1) if por_hora else timedelta(hours=25)
    puntos = []
    t = inicio
    while t <= ahora and len(puntos) < 5000:
        n = conteo.get(t, [0, 0, 0])
        puntos.append({"t": int(t.timestamp() * 1000), "sesiones": n[0], "intentos": n[1], "aprobadas": n[2]})
        t = bloque(t + paso)
    return {"unidad": "hora" if por_hora else "dia", "puntos": puntos}


async def sesiones_pagina(proyecto: str | None, dias: int, pagina: int,
                          resultado: str | None = None, buscar: str | None = None) -> dict:
    """Una pagina de la tabla, la sesion mas reciente primero."""
    where, params = _filtros(proyecto, dias, resultado, buscar)
    pool = db.pool()
    total = await pool.fetchval(f"SELECT count(*) FROM sesiones s {where}", *params)
    paginas = max(1, -(-total // POR_PAGINA))
    pagina = min(max(1, pagina), paginas)
    rows = await pool.fetch(
        f"""
        SELECT s.id, p.nombre AS proyecto, s.referencia_externa, s.estado, {_RESULTADO} AS resultado,
               {_ENTREGA_FALLIDA} AS entrega_fallida, s.intentos, s.max_intentos, s.creada, s.finalizada
        FROM sesiones s JOIN proyectos p ON p.id = s.proyecto_id {where}
        ORDER BY s.creada DESC, s.id
        LIMIT {POR_PAGINA} OFFSET {(pagina - 1) * POR_PAGINA}
        """,
        *params,
    )
    filas = [{**dict(r), "creada": _iso(r["creada"]), "finalizada": _iso(r["finalizada"])} for r in rows]
    return {"pagina": pagina, "paginas": paginas, "por_pagina": POR_PAGINA, "total": total, "filas": filas}


async def detalle_sesion(sesion_id: str) -> dict | None:
    """Una sesion con cada intento y como se entrego al proyecto."""
    pool = db.pool()
    s = await pool.fetchrow(
        f"""
        SELECT s.*, p.nombre AS proyecto, {_RESULTADO} AS resultado, {_ENTREGA_FALLIDA} AS entrega_fallida
        FROM sesiones s JOIN proyectos p ON p.id = s.proyecto_id WHERE s.id = $1
        """,
        sesion_id,
    )
    if s is None:
        return None
    intentos = await pool.fetch(
        "SELECT numero, aprobado, checks_fallidos, duracion_ms, creado, respuesta FROM intentos"
        " WHERE sesion_id = $1 ORDER BY numero DESC",
        sesion_id,
    )
    sesion = {
        k: s[k] for k in ("id", "proyecto_id", "proyecto", "referencia_externa", "estado", "resultado", "aprobada",
                          "entrega_fallida", "intentos", "max_intentos", "url_retorno", "webhook_url",
                          "metadata", "entrega")
    }
    sesion["checks_fallidos"] = list(s["checks_fallidos"])
    for k in ("creada", "expira", "finalizada"):
        sesion[k] = _iso(s[k])
    return {
        "sesion": sesion,
        "intentos": [{**dict(r), "checks_fallidos": list(r["checks_fallidos"]), "creado": _iso(r["creado"])}
                     for r in intentos],
    }

"""Entrega del resultado al backend del proyecto: un POST con JSON firmado.

Cabeceras (mismo esquema que el servicio de huellas):
    X-Vida-Evento:  sesion.completada | sesion.expirada | sesion.cancelada
    X-Vida-Entrega: id unico del evento (para ignorar duplicados)
    X-Vida-Firma:   t=<unix>,v1=<hex HMAC-SHA256(webhook_secreto, "<t>." + cuerpo_crudo)>

Solo una respuesta 2xx confirma que el proyecto lo guardo; si no, se reintenta
un par de veces al momento.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone

import httpx

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")
TOLERANCIA_SEGUNDOS = 300
# Las pruebas sustituyen el transporte HTTP para simular el backend del proyecto.
transporte: httpx.AsyncBaseTransport | None = None


def firmar(secreto: str, timestamp: int, cuerpo: bytes) -> str:
    return hmac.new(secreto.encode(), f"{timestamp}.".encode() + cuerpo, hashlib.sha256).hexdigest()


def cabecera_firma(secreto: str, cuerpo: bytes, timestamp: int | None = None) -> str:
    t = int(timestamp or time.time())
    return f"t={t},v1={firmar(secreto, t, cuerpo)}"


def verificar(secreto: str, cabecera: str, cuerpo: bytes, tolerancia: int = TOLERANCIA_SEGUNDOS) -> bool:
    """Lo que debe hacer el backend receptor (ver README). Rechaza firmas viejas para evitar reenvios."""
    try:
        partes = dict(p.split("=", 1) for p in cabecera.split(","))
        t = int(partes["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > tolerancia:
        return False
    return hmac.compare_digest(firmar(secreto, t, cuerpo), partes.get("v1", ""))


async def enviar(url: str, secreto: str, evento: str, datos: dict, reintentos: int | None = None) -> dict:
    """Envia y espera la confirmacion. Devuelve {ok, codigo, error, intentos, id}."""
    reintentos = settings.delivery_retries if reintentos is None else reintentos
    cuerpo_evento = {
        "id": "evt_" + uuid.uuid4().hex,
        "evento": evento,
        "creado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "datos": datos,
    }
    cuerpo = json.dumps(cuerpo_evento, ensure_ascii=False).encode()
    estado = {"ok": False, "codigo": None, "error": None, "intentos": 0, "id": cuerpo_evento["id"]}

    async with httpx.AsyncClient(transport=transporte, timeout=settings.delivery_timeout_sec) as cliente:
        for intento in range(reintentos + 1):
            if intento:
                await asyncio.sleep(intento)  # 1 s, 2 s...
            estado["intentos"] = intento + 1
            # Se firma en cada intento: un reintento tardio no debe caer fuera de la tolerancia.
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "prueba-de-vida/1.0",
                "X-Vida-Evento": evento,
                "X-Vida-Entrega": cuerpo_evento["id"],
                "X-Vida-Firma": cabecera_firma(secreto, cuerpo),
            }
            try:
                r = await cliente.post(url, content=cuerpo, headers=headers)
                estado["codigo"] = r.status_code
                if 200 <= r.status_code < 300:
                    estado.update(ok=True, error=None)
                    logger.info("Evento %s entregado a %s", evento, url)
                    return estado
                estado["error"] = f"HTTP {r.status_code}: {r.text[:200]}"
            except httpx.HTTPError as e:
                estado["error"] = f"{type(e).__name__}: {e}"
            logger.warning("Entrega de %s a %s fallo (intento %d): %s", evento, url, intento + 1, estado["error"])
    return estado

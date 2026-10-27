"""Login del panel administrativo (/panel).

- Una sola cuenta: ADMIN_EMAIL + ADMIN_PASSWORD_HASH (scrypt; la contraseña
  nunca se guarda en claro).
- La sesion es una cookie "payload.firma" con HMAC-SHA256: el navegador la
  guarda pero no la puede alterar. Es HttpOnly, SameSite=Strict y Secure con HTTPS.

Generar el hash de una contraseña:  python -m app.core.acceso hash
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import Request
from fastapi.responses import Response

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

COOKIE_ADMIN = "pv_admin"

if settings.secret_key:
    _SECRET = settings.secret_key.encode()
else:
    _SECRET = secrets.token_bytes(32)
    logger.warning("SECRET_KEY vacio: se genero una al arrancar; hay que volver a entrar al panel tras reiniciar.")


# ---------------------------------------------------------------- firma

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


def firmar(payload: dict) -> str:
    cuerpo = _b64(json.dumps(payload, separators=(",", ":")).encode())
    firma = _b64(hmac.new(_SECRET, cuerpo.encode(), hashlib.sha256).digest())
    return f"{cuerpo}.{firma}"


def leer(valor: str | None) -> dict | None:
    """Payload de una cookie firmada, o None si no es valida o ya vencio."""
    if not valor or "." not in valor or len(valor) > 2048:
        return None
    cuerpo, firma = valor.rsplit(".", 1)
    esperada = _b64(hmac.new(_SECRET, cuerpo.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(firma.encode(), esperada.encode()):
        return None
    try:
        payload = json.loads(_unb64(cuerpo))
    except Exception:
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


# ---------------------------------------------------------------- contraseña

# scrypt con los parametros recomendados para logins interactivos.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(dk)}"


def verificar_password(password: str, guardado: str) -> bool:
    try:
        esquema, salt, dk = guardado.split("$")
        if esquema != "scrypt":
            return False
        calculado = hashlib.scrypt(password.encode(), salt=_unb64(salt), n=_N, r=_R, p=_P, dklen=32)
        return hmac.compare_digest(calculado, _unb64(dk))
    except Exception:
        return False


# Hash fijo para comparar cuando el correo no coincide: asi el tiempo de
# respuesta no revela si el correo existe.
_HASH_SEÑUELO = hash_password(secrets.token_urlsafe(16))


def credenciales_validas(email: str, password: str) -> bool:
    if not settings.admin_email or not settings.admin_password_hash:
        return False
    correo_ok = hmac.compare_digest(email.strip().lower().encode(), settings.admin_email.encode())
    pass_ok = verificar_password(password, settings.admin_password_hash if correo_ok else _HASH_SEÑUELO)
    return correo_ok and pass_ok


# ---------------------------------------------------------------- cookie

def _es_https(request: Request) -> bool:
    # Detras de un proxy, uvicorn --proxy-headers ya pone el esquema real
    # (si la IP del proxy esta en FORWARDED_ALLOW_IPS).
    return request.url.scheme == "https"


def abrir_sesion(response: Response, request: Request) -> None:
    segundos = settings.admin_session_hours * 3600
    valor = firmar({"t": "a", "exp": int(time.time()) + segundos})
    response.set_cookie(COOKIE_ADMIN, valor, max_age=segundos, httponly=True,
                        secure=_es_https(request), samesite="strict", path="/")


def cerrar_sesion(response: Response) -> None:
    response.delete_cookie(COOKIE_ADMIN, path="/")


def es_admin(request: Request) -> bool:
    p = leer(request.cookies.get(COOKIE_ADMIN))
    return bool(p and p.get("t") == "a")


if __name__ == "__main__":
    import getpass
    import sys

    if sys.argv[1:] != ["hash"]:
        print("Uso: python -m app.core.acceso hash")
        sys.exit(1)
    pw = getpass.getpass("Contraseña: ")
    if pw != getpass.getpass("Repite la contraseña: "):
        print("No coinciden.")
        sys.exit(1)
    print(hash_password(pw))

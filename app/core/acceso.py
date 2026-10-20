"""Acceso a la demo: login del panel y entrada por el codigo QR.

- El panel (/panel) es para una sola cuenta: ADMIN_EMAIL + ADMIN_PASSWORD_HASH.
- Quien escanea el QR (/p/<token>) recibe una cookie de participante con su
  sesion y la huella del QR con que entro: si se genera un QR nuevo, esas
  cookies dejan de servir.
- Las cookies son "payload.firma" con HMAC-SHA256: el navegador las guarda pero
  no las puede alterar. Son HttpOnly y SameSite, y Secure cuando hay HTTPS.

Generar el hash de una contraseña:  python -m app.core.acceso hash
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import HTTPException, Request, Security
from fastapi.responses import Response
from fastapi.security import APIKeyHeader

from app.core.config import settings

logger = logging.getLogger("uvicorn.error")

COOKIE_ADMIN = "pv_admin"
COOKIE_ACCESO = "pv_acceso"

if settings.secret_key:
    _SECRET = settings.secret_key.encode()
else:
    _SECRET = secrets.token_bytes(32)
    logger.warning("SECRET_KEY vacio: se genero una al arrancar; las sesiones se pierden al reiniciar.")

# Huella del QR vigente; la actualiza app.web al arrancar y al generar uno nuevo.
_qr_huella: str | None = None


def huella_qr(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def set_qr_vigente(token: str) -> None:
    global _qr_huella
    _qr_huella = huella_qr(token)


def token_qr_valido(token: str) -> bool:
    return _qr_huella is not None and hmac.compare_digest(huella_qr(token).encode(), _qr_huella.encode())


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


# ---------------------------------------------------------------- cookies

def _es_https(request: Request) -> bool:
    # Detras de un proxy, uvicorn --proxy-headers ya pone el esquema real
    # (si la IP del proxy esta en FORWARDED_ALLOW_IPS).
    return request.url.scheme == "https"


def poner_cookie(response: Response, request: Request, nombre: str, payload: dict, segundos: int,
                 samesite: str) -> None:
    payload = {**payload, "exp": int(time.time()) + segundos}
    response.set_cookie(nombre, firmar(payload), max_age=segundos, httponly=True,
                        secure=_es_https(request), samesite=samesite, path="/")


def es_admin(request: Request) -> bool:
    p = leer(request.cookies.get(COOKIE_ADMIN))
    return bool(p and p.get("t") == "a")


def sesion_participante(request: Request) -> str | None:
    """Id de sesion de quien entro con el QR vigente, o None."""
    p = leer(request.cookies.get(COOKIE_ACCESO))
    if not p or p.get("t") != "p" or not isinstance(p.get("sid"), str):
        return None
    if _qr_huella is None or not hmac.compare_digest(str(p.get("q", "")).encode(), _qr_huella.encode()):
        return None
    return p["sid"]


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_acceso(request: Request, api_key: str | None = Security(_api_key_header)) -> str:
    """Para la API: entra quien escaneo el QR, el admin del panel o un proyecto con API key.

    Deja en request.state.client quien es: el rate limit cuenta por sesion y no
    por IP, porque en una presentacion todos comparten la misma red.
    """
    sid = sesion_participante(request)
    if sid:
        request.state.client = f"ses:{sid}"
        request.state.sesion = sid
        return request.state.client
    if es_admin(request):
        request.state.client = "admin"
        return "admin"
    if api_key and settings.api_keys:
        given = api_key.encode()
        for name, key in settings.api_keys.items():
            # Comparacion en tiempo constante: no filtra cuantos caracteres coinciden.
            if hmac.compare_digest(given, key.encode()):
                request.state.client = name
                return name
    raise HTTPException(status_code=401, detail="Acceso no válido. Escanea el código QR para entrar.")


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

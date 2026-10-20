"""Middleware de seguridad: tamaño maximo del request y encabezados del navegador.

Son ASGI puros (no BaseHTTPMiddleware) para no cargar el body completo en memoria
y no estorbar respuestas grandes.
"""
import json

from app.core.config import settings

_MB = 1024 * 1024


class _Demasiado(Exception):
    pass


async def _responder_413(send) -> None:
    body = json.dumps({"detail": "La solicitud es demasiado grande."}).encode()
    await send({"type": "http.response.start", "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


class BodyLimit:
    """Corta el request en cuanto pasa del limite, antes de que FastAPI lo guarde.

    Sin esto, Starlette recibe todo el multipart (a disco temporal) y recien
    despues se revisa el tamaño de cada archivo: alguien podria llenar el disco.
    """

    def __init__(self, app):
        self.app = app
        # Video + INE + margen del multipart; formularios (login) mucho menos.
        self.limite_api = (settings.max_video_mb + settings.max_image_mb * 2 + 1) * _MB
        self.limite_form = 16 * 1024

    def _limite(self, path: str) -> int:
        return self.limite_api if path.startswith("/api/") else self.limite_form

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] in ("GET", "HEAD", "OPTIONS"):
            await self.app(scope, receive, send)
            return

        limite = self._limite(scope["path"])
        for nombre, valor in scope["headers"]:
            if nombre == b"content-length":
                try:
                    if int(valor) > limite:
                        await _responder_413(send)
                        return
                except ValueError:
                    await _responder_413(send)
                    return

        recibido = 0
        empezo = False

        async def receive_limitado():
            nonlocal recibido
            msg = await receive()
            if msg["type"] == "http.request":
                recibido += len(msg.get("body", b""))
                if recibido > limite:
                    raise _Demasiado()
            return msg

        async def send_marcado(msg):
            nonlocal empezo
            if msg["type"] == "http.response.start":
                empezo = True
            await send(msg)

        try:
            await self.app(scope, receive_limitado, send_marcado)
        except _Demasiado:
            if not empezo:
                await _responder_413(send)


# MediaPipe (detector de gestos) se carga de jsDelivr y sus modelos de Google
# Storage; compila WebAssembly. Todo lo demas sale del propio sitio.
_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src https://fonts.gstatic.com",
    "img-src 'self' data: blob:",
    "media-src 'self' blob:",
    "connect-src 'self' https://cdn.jsdelivr.net https://storage.googleapis.com",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "object-src 'none'",
])

_ENCABEZADOS = [
    (b"content-security-policy", _CSP.encode()),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    # El token del QR va en la URL de /p/<token>: no debe salir en el Referer.
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(self), microphone=(self), geolocation=(), payment=(), usb=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_con_encabezados(msg):
            if msg["type"] == "http.response.start":
                headers = list(msg.get("headers", []))
                presentes = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in _ENCABEZADOS if k not in presentes]
                # Las paginas y la API no se guardan en cache (llevan sesion o resultados).
                if not scope["path"].startswith("/static/") and b"cache-control" not in presentes:
                    headers.append((b"cache-control", b"no-store"))
                msg = {**msg, "headers": headers}
            await send(msg)

        await self.app(scope, receive, send_con_encabezados)

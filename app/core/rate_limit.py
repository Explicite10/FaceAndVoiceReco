import math
import threading
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request

from app.core.config import settings

# Por encima de estos clientes registrados se limpian los que ya no tienen hits.
_PURGE_AT = 10_000


class RateLimiter:
    """Ventana deslizante en memoria. Se usa como dependencia de FastAPI.

    Cuenta por cliente si require_acceso ya lo identifico (sesion del QR o
    proyecto); si no, por IP. Con `key` se cuenta por lo que esa funcion devuelva.
    """

    def __init__(self, limit: int, window_sec: float, key: Callable[[Request], str] | None = None):
        self.limit = limit
        self.window = window_sec
        self._key = key
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        if self.limit <= 0:
            return

        client = getattr(request.state, "client", None)
        if self._key is not None:
            key = self._key(request)
        elif client:
            key = f"key:{client}"
        else:
            key = client_ip(request)
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and hits[0] <= now - self.window:
                hits.popleft()

            if len(hits) >= self.limit:
                retry_after = max(1, math.ceil(hits[0] + self.window - now))
                raise HTTPException(
                    status_code=429,
                    detail=f"Demasiadas solicitudes. Intenta de nuevo en {retry_after} s.",
                    headers={"Retry-After": str(retry_after)},
                )

            hits.append(now)
            if len(self._hits) > _PURGE_AT:
                self._purge(now)

    def _purge(self, now: float) -> None:
        stale = [k for k, h in self._hits.items() if not h or h[-1] <= now - self.window]
        for k in stale:
            del self._hits[k]


def client_ip(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'unknown'}"


# Un solo presupuesto para ambos endpoints: los dos consumen el mismo CPU.
verification_limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_sec)

# Pedir un reto es barato, pero cada uno ocupa memoria hasta que vence.
challenge_limiter = RateLimiter(settings.challenge_rate_limit_requests, settings.rate_limit_window_sec)

# Login del panel: por IP y en total, para frenar fuerza bruta aunque venga de muchas IPs.
login_ip_limiter = RateLimiter(settings.login_attempts_per_ip, settings.login_window_sec, key=client_ip)
login_global_limiter = RateLimiter(settings.login_attempts_global, settings.login_window_sec, key=lambda r: "login")

# Escanear el QR crea una sesion nueva: se limita por IP para que no se fabriquen sesiones en masa.
qr_scan_limiter = RateLimiter(settings.qr_scans_per_ip, settings.qr_scan_window_sec, key=client_ip)

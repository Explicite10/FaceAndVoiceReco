import math
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

from app.core.config import settings

# Por encima de estos clientes registrados se limpian los que ya no tienen hits.
_PURGE_AT = 10_000


class RateLimiter:
    """Ventana deslizante en memoria. Se usa como dependencia de FastAPI.

    Cuenta por proyecto si require_api_key ya lo identifico; si no, por IP.
    """

    def __init__(self, limit: int, window_sec: float):
        self.limit = limit
        self.window = window_sec
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        if self.limit <= 0:
            return

        project = getattr(request.state, "client", None)
        if project:
            key = f"key:{project}"
        else:
            key = f"ip:{request.client.host if request.client else 'unknown'}"
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


# Un solo presupuesto para ambos endpoints: los dos consumen el mismo CPU.
verification_limiter = RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_sec)

# Pedir un reto es barato, pero cada uno ocupa memoria hasta que vence.
challenge_limiter = RateLimiter(settings.challenge_rate_limit_requests, settings.rate_limit_window_sec)

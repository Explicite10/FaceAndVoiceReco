"""Retos de un solo uso: frase y gestos aleatorios generados por el servidor.

El cliente pide un reto, graba el video siguiendolo y solo devuelve el
challenge_id: asi no puede elegir la frase ni reusar un video grabado antes.
Se guardan en memoria, igual que el rate limit: vale con un solo worker.
"""
import secrets
import threading
import time

from app.core.config import settings
from app.services.errors import CheckError

# Palabras comunes, sin homofonos ni numeros (Whisper escribe los numeros como
# digitos y "379" no se normaliza igual que "tres siete nueve").
_WORDS = (
    "manzana", "caballo", "montaña", "ventana", "guitarra", "naranja", "tortuga",
    "camino", "cuaderno", "pelota", "bandera", "estrella", "girasol", "mariposa",
    "puente", "sombrero", "elefante", "cereza", "jardín", "tormenta", "cuchara",
    "bicicleta", "abeja", "planeta", "zapato", "galleta", "isla", "volcán",
    "paraguas", "castillo", "delfín", "pirámide", "chocolate", "escalera",
    "tambor", "ballena", "semilla", "cometa", "lámpara", "pingüino",
)
_WORDS_PER_PHRASE = 3

# Un gesto de cada tipo; el orden y el lado se sortean.
GESTURE_LABELS = {
    "turn_left": "Gira la cabeza a tu izquierda",
    "turn_right": "Gira la cabeza a tu derecha",
    "blink": "Parpadea despacio",
    "raise_left_hand": "Alza tu mano izquierda junto a la cara",
    "raise_right_hand": "Alza tu mano derecha junto a la cara",
}

_pending: dict[str, dict] = {}
_lock = threading.Lock()


def _random_phrase() -> str:
    words = secrets.SystemRandom().sample(_WORDS, _WORDS_PER_PHRASE)
    return f"confirmo mi identidad con las palabras {words[0]}, {words[1]} y {words[2]}"


def _random_gestures() -> list[str]:
    rng = secrets.SystemRandom()
    gestures = [
        rng.choice(("turn_left", "turn_right")),
        "blink",
        rng.choice(("raise_left_hand", "raise_right_hand")),
    ]
    rng.shuffle(gestures)
    return gestures


def _purge(now: float) -> None:
    for cid in [cid for cid, c in _pending.items() if c["expires_at"] <= now]:
        del _pending[cid]


def create_challenge(client: str | None) -> dict:
    now = time.time()
    challenge = {
        "challenge_id": secrets.token_urlsafe(24),
        "phrase": _random_phrase(),
        "gestures": _random_gestures(),
        "expires_at": now + settings.challenge_ttl_sec,
        "client": client,
    }
    with _lock:
        _purge(now)
        if len(_pending) >= settings.challenge_max_pending:
            raise CheckError(503, "challenge_capacity", "Hay demasiados retos pendientes. Intenta en unos minutos.")
        _pending[challenge["challenge_id"]] = challenge
    return challenge


def consume_challenge(challenge_id: str, client: str | None) -> dict:
    """Devuelve el reto y lo borra: un reto solo sirve para un video."""
    with _lock:
        challenge = _pending.pop(challenge_id, None)
    # Un reto de otro proyecto se trata como inexistente, sin revelar que existe.
    if challenge is None or challenge["client"] != client:
        raise CheckError(400, "challenge_invalid", "El reto no existe o ya se usó. Pide uno nuevo.")
    if challenge["expires_at"] <= time.time():
        raise CheckError(400, "challenge_expired", "El reto expiró. Pide uno nuevo.")
    return challenge

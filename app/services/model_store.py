"""Descarga de modelos verificada por SHA-256 (una descarga corrupta o un modelo
cambiado en el origen falla en vez de cargarse en silencio)."""
import hashlib
import logging
import os
import urllib.request

logger = logging.getLogger("uvicorn.error")

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.getenv("FACE_MODELS_DIR", os.path.join(_PROJECT_ROOT, "models"))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_model(filename: str, url: str, sha256: str) -> str:
    path = os.path.join(MODELS_DIR, filename)
    if os.path.exists(path) and _sha256(path) == sha256:
        return path

    os.makedirs(MODELS_DIR, exist_ok=True)
    tmp = path + ".part"
    logger.info("Modelos: descargando %s", filename)
    urllib.request.urlretrieve(url, tmp)
    if _sha256(tmp) != sha256:
        os.remove(tmp)
        raise RuntimeError(f"Checksum invalido para {filename}; descarga corrupta o modelo cambiado.")
    os.replace(tmp, path)
    return path

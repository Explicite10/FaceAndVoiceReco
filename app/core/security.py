import hmac

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app import db
from app.core.config import settings
from app.services import proyectos

# Con auto_error=False el error lo damos nosotros, y Swagger muestra el boton "Authorize".
_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Llave del proyecto que consume la API."
)


async def identify_project(api_key: str | None) -> tuple[str, dict | None] | None:
    """(nombre para el rate limit, proyecto de la base o None) si la llave es valida."""
    if not api_key:
        return None
    if db.enabled():
        proyecto = await proyectos.por_api_key(api_key)
        if proyecto:
            return proyecto["id"], proyecto
    given = api_key.encode()
    for name, key in settings.api_keys.items():
        # Comparacion en tiempo constante: no filtra cuantos caracteres coinciden.
        if hmac.compare_digest(given, key.encode()):
            return name, None
    return None


async def require_api_key(request: Request, api_key: str | None = Security(_api_key_header)) -> str | None:
    """API directa: identifica al proyecto por su llave y lo deja en request.state.client.

    Acepta proyectos de la base (python -m app.cli) y llaves de API_KEYS. Solo en
    modo DEMO y sin API_KEYS se deja pasar sin llave. El rate limit cuenta por proyecto.
    """
    found = await identify_project(api_key)
    if found:
        request.state.client = found[0]
        return found[0]
    if settings.demo and not settings.api_keys and not api_key:
        return None
    raise HTTPException(status_code=401, detail="Falta la API key o no es válida (header X-API-Key).")


async def require_project(request: Request, api_key: str | None = Security(_api_key_header)) -> dict:
    """Sesiones: solo proyectos dados de alta en la base."""
    found = await identify_project(api_key)
    if not found or found[1] is None:
        raise HTTPException(status_code=401, detail="Falta la API key o no es válida (header X-API-Key).")
    request.state.client = found[0]
    return found[1]

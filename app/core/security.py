import hmac

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from app.core.config import settings

# Con auto_error=False el error lo damos nosotros, y Swagger muestra el boton "Authorize".
_api_key_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Llave del proyecto que consume la API."
)


def require_api_key(request: Request, api_key: str | None = Security(_api_key_header)) -> str | None:
    """Identifica al proyecto por su llave y lo deja en request.state.client.

    El rate limit cuenta por ese proyecto. Sin llaves configuradas no se exige nada.
    """
    if not settings.api_keys:
        return None

    if api_key:
        given = api_key.encode()
        for name, key in settings.api_keys.items():
            # Comparacion en tiempo constante: no filtra cuantos caracteres coinciden.
            if hmac.compare_digest(given, key.encode()):
                request.state.client = name
                return name

    raise HTTPException(status_code=401, detail="Falta la API key o no es válida (header X-API-Key).")

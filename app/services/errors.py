from fastapi import HTTPException


class CheckError(HTTPException):
    """Falla de un analisis con un codigo estable, para que el cliente no dependa del texto."""

    def __init__(self, status_code: int, code: str, detail: str):
        super().__init__(status_code=status_code, detail=detail)
        self.code = code

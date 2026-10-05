import os
import uuid

import aiofiles
from fastapi import UploadFile, HTTPException


# .webm es lo que graba MediaRecorder en Chrome/Firefox; Safari da .mp4.
ALLOWED_VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


async def save_upload_to_temp(
    upload: UploadFile,
    upload_dir: str,
    *,
    allowed_ext: set[str] | None = None,
    max_mb: int | None = 80,
) -> str:
    """Guarda UploadFile a disco por chunks y retorna el path.

    Valida extensión si allowed_ext se pasa.
    """

    os.makedirs(upload_dir, exist_ok=True)

    filename = upload.filename or "file"
    ext = os.path.splitext(filename)[1].lower()

    if allowed_ext is not None and ext not in allowed_ext:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado: {ext}. Permitidos: {', '.join(sorted(allowed_ext))}",
        )

    temp_name = f"{uuid.uuid4().hex}{ext}"
    temp_path = os.path.join(upload_dir, temp_name)

    size = 0
    limit_bytes = (max_mb * 1024 * 1024) if max_mb is not None else None

    async with aiofiles.open(temp_path, "wb") as out:
        while True:
            chunk = await upload.read(1024 * 1024)  # 1MB
            if not chunk:
                break
            size += len(chunk)
            if limit_bytes is not None and size > limit_bytes:
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail=f"Video demasiado grande (>{max_mb}MB).")
            await out.write(chunk)

    return temp_path

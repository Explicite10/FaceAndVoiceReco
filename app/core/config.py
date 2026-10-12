import os

from pydantic import BaseModel

_MIN_KEY_LEN = 24


def _parse_api_keys(raw: str) -> dict[str, str]:
    """API_KEYS="proyecto1:llave1,proyecto2:llave2" -> {"proyecto1": "llave1", ...}.

    Falla al arrancar si esta mal formado: mejor no levantar que quedar abierta.
    """
    keys: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, key = (x.strip() for x in item.partition(":"))
        if not sep or not name or not key:
            raise ValueError(f"API_KEYS mal formado cerca de '{name}': usa proyecto:llave separados por coma.")
        if len(key) < _MIN_KEY_LEN:
            raise ValueError(f"La llave de '{name}' es muy corta (minimo {_MIN_KEY_LEN} caracteres).")
        if name in keys:
            raise ValueError(f"El proyecto '{name}' aparece dos veces en API_KEYS.")
        if key in keys.values():
            raise ValueError(f"La llave de '{name}' esta repetida en otro proyecto.")
        keys[name] = key
    return keys


def _parse_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


class Settings(BaseModel):
    app_name: str = "Verificacion de identidad"

    # Rostro (YuNet + SFace). 0.363 es el umbral coseno publicado por OpenCV
    # para SFace; recalibrar con pares reales INE + video.
    sim_threshold: float = 0.363        # centro de la escala de %: ahi el porcentaje vale 50
    face_segments: int = 3              # tramos del video comparados contra la ID
    # Solo en el video (la INE trae una segunda foto "fantasma"). Se rechaza si hay
    # mas de una cara en al menos N de los cuadros analizados (~6 por segundo).
    multi_face_min_frames: int = 3

    # Porcentaje mostrado: sigmoide centrada en sim_threshold.
    score_k: float = 18.0
    # Porcentaje minimo para que la cara coincida (en cada tramo del video, o en
    # la foto). Cada request puede mandar el suyo en match_threshold_percent.
    # 70% equivale a similitud coseno ~0.41; 50% a 0.363.
    face_match_percent: float = float(os.getenv("FACE_MATCH_PERCENT", "70"))

    # ---------- Audio / voice liveness ----------
    # Frase temporal hardcodeada. El endpoint acepta override por request para
    # migrar a challenge-response (frase aleatoria por sesion) sin tocar codigo.
    expected_phrase: str = os.getenv("EXPECTED_PHRASE") or "yo confirmo mi identidad y autorizo la verificacion de mi cuenta el dia de hoy"

    # Perillas de despliegue: cambian por entorno sin tocar codigo.
    asr_model_size: str = os.getenv("ASR_MODEL_SIZE", "small")      # tiny|base|small|medium|large-v3
    asr_device: str = os.getenv("ASR_DEVICE", "cpu")                # "cuda" en el servidor con GPU
    asr_compute_type: str = os.getenv("ASR_COMPUTE_TYPE", "int8")   # "float16" con GPU
    asr_cpu_threads: int = int(os.getenv("ASR_CPU_THREADS", "4"))
    asr_language: str = "es"             # fijo: el autodetect falla en clips cortos
    asr_beam_size: int = 5
    asr_max_concurrency: int = int(os.getenv("ASR_MAX_CONCURRENCY", "1"))  # = cores / asr_cpu_threads

    # Gates de audio (antes y despues del ASR)
    audio_min_duration_sec: float = 4.0
    audio_max_duration_sec: float = 20.0
    min_speech_duration_sec: float = 2.5
    min_speech_ratio: float = 0.15

    # Gates de confianza del ASR (anti-alucinacion)
    max_no_speech_prob: float = 0.6
    min_avg_logprob: float = -1.0

    # Scoring de la frase
    phrase_match_threshold: float = 85.0   # match si match_percent >=
    wer_weight: float = 0.7                # peso WER vs fuzzy ratio

    # ---------- Prueba de vida ----------
    # Detector de pantallas/fotos (Silent-Face): probabilidad promedio de "real".
    antispoof_threshold: float = 0.5
    # Sincronia labios-voz (MediaPipe). Valores PROVISIONALES: calibrar con
    # videos reales (lectura normal vs. pantalla, foto o audio doblado).
    lip_min_tracked_ratio: float = 0.6     # cara visible en al menos este % de cuadros
    lip_motion_min: float = 0.004          # movimiento medio de la boca al hablar
    lip_corr_min: float = 0.2              # correlacion boca vs. energia de la voz

    # ---------- Foto contra identificacion ----------
    # Calidad minima de la cara en la foto. Valores PROVISIONALES: calibrar con
    # fotos reales aceptadas y rechazadas.
    photo_min_face_px: int = 112           # lado menor de la cara; SFace trabaja a 112 px
    photo_min_sharpness: float = 30.0      # varianza del Laplaciano de la cara a 112 px
    photo_min_brightness: float = 50.0     # brillo medio de la cara (0-255)
    photo_max_brightness: float = 210.0

    # ---------- Acceso ----------
    # Una llave por proyecto que consume la API (header X-API-Key). Vacio =
    # sin autenticacion, solo para desarrollo: al arrancar se advierte en el log.
    api_keys: dict[str, str] = _parse_api_keys(os.getenv("API_KEYS", ""))
    # Origenes de navegador permitidos, p. ej. "https://app1.com,https://app2.com".
    # Solo hace falta si un frontend llama directo a la API; vacio = sin CORS.
    cors_origins: list[str] = _parse_list(os.getenv("CORS_ORIGINS", ""))

    # ---------- Limites de uso ----------
    # Por proyecto (API key); sin llaves configuradas, por IP del cliente. Detras
    # de un proxy, la IP sale de X-Forwarded-For (ver FORWARDED_ALLOW_IPS).
    # Se cuenta en memoria: vale con un solo worker de uvicorn.
    rate_limit_requests: int = int(os.getenv("RATE_LIMIT_REQUESTS", "10"))  # 0 = sin limite
    rate_limit_window_sec: int = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))

    # Subidas. Ojo: FastAPI recibe el request completo antes de estas
    # validaciones; si hay un proxy al frente, conviene que limite el body.
    max_video_mb: int = int(os.getenv("MAX_VIDEO_MB", "50"))   # 20 s a 1080p pesan ~45 MB
    max_image_mb: int = int(os.getenv("MAX_IMAGE_MB", "10"))
    max_image_megapixels: int = 50   # se revisa en el encabezado, antes de decodificar

settings = Settings()

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
    # Frase fija, solo cuando REQUIRE_CHALLENGE=false y la solicitud no trae
    # challenge_id. Con reto, la frase la genera el servidor (ver challenge.py).
    expected_phrase: str = os.getenv("EXPECTED_PHRASE") or "yo confirmo mi identidad y autorizo la verificacion de mi cuenta el dia de hoy"

    # Perillas de despliegue: cambian por entorno sin tocar codigo.
    asr_model_size: str = os.getenv("ASR_MODEL_SIZE", "small")      # tiny|base|small|medium|large-v3
    asr_device: str = os.getenv("ASR_DEVICE", "cpu")                # "cuda" en el servidor con GPU
    asr_compute_type: str = os.getenv("ASR_COMPUTE_TYPE", "int8")   # "float16" con GPU
    asr_cpu_threads: int = int(os.getenv("ASR_CPU_THREADS", "4"))
    asr_language: str = "es"             # fijo: el autodetect falla en clips cortos
    asr_beam_size: int = 5
    asr_max_concurrency: int = int(os.getenv("ASR_MAX_CONCURRENCY", "1"))  # = cores / asr_cpu_threads

    # Gates de audio (antes y despues del ASR). El maximo cubre los gestos del
    # reto (~10 s) mas la lectura de la frase (hasta 50 palabras, ~30 s).
    audio_min_duration_sec: float = 4.0
    audio_max_duration_sec: float = 60.0
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
    # Movimiento medio de la boca al hablar (apertura / alto de la cara, por cuadro).
    # Depende de la distancia y la camara: una persona real dio 0.003-0.005 y una
    # foto ronda 0.0003. Por eso pesa mas la comparacion contra el silencio.
    lip_motion_min: float = 0.0015
    lip_motion_ratio_min: float = 3.0      # al hablar, al menos 3x lo que se mueve en silencio
    lip_corr_min: float = 0.2              # correlacion boca vs. energia de la voz

    # ---------- Reto (challenge-response) ----------
    # El servidor genera frase y gestos aleatorios de un solo uso; el cliente
    # solo devuelve el challenge_id. Sin reto se usa la frase fija (EXPECTED_PHRASE)
    # y no se piden gestos: solo para desarrollo o migracion.
    require_challenge: bool = os.getenv("REQUIRE_CHALLENGE", "true").lower() not in ("0", "false", "no")
    challenge_ttl_sec: int = int(os.getenv("CHALLENGE_TTL_SEC", "180"))
    challenge_max_pending: int = 10_000    # tope de retos vivos en memoria
    challenge_rate_limit_requests: int = int(os.getenv("CHALLENGE_RATE_LIMIT_REQUESTS", "30"))
    # Deteccion de gestos (MediaPipe). Valores PROVISIONALES: calibrar con
    # videos reales. El navegador usa los mismos para guiar al usuario.
    # Giro: desplazamiento de la nariz respecto al centro de la cara, como
    # fraccion del ancho de la cara (0 = de frente; ~0.12 son unos 20 grados).
    gesture_turn_offset: float = 0.12
    gesture_turn_min_frames: int = 3       # cuadros seguidos a 15 fps (~0.2 s)
    gesture_blink_closed: float = 0.5      # blendshape eyeBlink promedio: ojos cerrados
    gesture_blink_open: float = 0.3        # ... y abiertos antes y despues
    gesture_hand_min_frames: int = 2       # cuadros seguidos con la mano arriba (~7.5 fps)

    # ---------- Foto contra identificacion ----------
    # Calidad minima de la cara en la foto. Valores PROVISIONALES: calibrar con
    # fotos reales aceptadas y rechazadas.
    photo_min_face_px: int = 112           # lado menor de la cara; SFace trabaja a 112 px
    photo_min_sharpness: float = 30.0      # varianza del Laplaciano de la cara a 112 px
    photo_min_brightness: float = 50.0     # brillo medio de la cara (0-255)
    photo_max_brightness: float = 210.0

    # ---------- Sesiones para proyectos autorizados ----------
    # Proyectos y resumen de sesiones (sin biometria). Sin DATABASE_URL la API
    # directa sigue funcionando, pero no hay sesiones.
    database_url: str | None = os.getenv("DATABASE_URL") or None
    # URL publica con la que el usuario abre la captura; si no, la de la peticion.
    public_url: str | None = (os.getenv("PUBLIC_URL") or "").rstrip("/") or None
    session_minutes: int = int(os.getenv("SESSION_MINUTES", "30"))
    session_max_attempts: int = int(os.getenv("SESSION_MAX_ATTEMPTS", "3"))
    # Analisis de video simultaneos: cada uno ocupa varios nucleos por un rato.
    max_concurrent_verifications: int = int(os.getenv("MAX_CONCURRENT_VERIFICATIONS", "1"))
    # Vigencia de las ligas de evidencia (video, fotogramas, cara de la INE) que
    # trae el webhook cuando la sesion las pide. Minimo 30 minutos.
    evidence_minutes: int = max(30, int(os.getenv("EVIDENCE_MINUTES", "60")))
    delivery_timeout_sec: float = float(os.getenv("DELIVERY_TIMEOUT_SEC", "20"))
    delivery_retries: int = int(os.getenv("DELIVERY_RETRIES", "2"))
    # Acepta webhook_url / url_retorno con http:// (solo desarrollo local).
    allow_http_urls: bool = os.getenv("ALLOW_HTTP_URLS", "false").lower() in ("1", "true", "yes")
    # Pagina de prueba completa en "/" y API directa abierta sin llaves, solo
    # para desarrollo. Apagado por defecto: olvidar la variable no abre la API.
    demo: bool = os.getenv("DEMO", "false").lower() in ("1", "true", "yes")

    # ---------- Acceso ----------
    # Llaves fijas por variable de entorno (opcional). Los proyectos normales se
    # dan de alta en la base de datos con `python -m app.cli`.
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

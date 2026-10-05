import os

from pydantic import BaseModel

class Settings(BaseModel):
    app_name: str = "Verificacion de identidad"

    # Rostro (YuNet + SFace). 0.363 es el umbral coseno publicado por OpenCV
    # para SFace; recalibrar con pares reales INE + video.
    sim_threshold: float = 0.363        # match si similitud >= (en cada tramo)
    face_segments: int = 3              # tramos del video comparados contra la ID
    # Solo en el video (la INE trae una segunda foto "fantasma"). Se rechaza si hay
    # mas de una cara en al menos N de los cuadros analizados (~6 por segundo).
    multi_face_min_frames: int = 3

    # Porcentaje mostrado: sigmoide centrada en el umbral (match <=> >= 50%).
    score_k: float = 18.0

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

settings = Settings()

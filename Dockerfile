# amd64 fijo: mediapipe 0.10.35 no publica paquete para Linux ARM, y la 1.0.x
# aborta el proceso en macOS. Asi la imagen es la misma en cualquier equipo.
FROM --platform=linux/amd64 python:3.12-slim

# Evita prompts, mejora logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Dependencias del sistema:
# - libgl1 y libglib2.0-0: necesarias para OpenCV
# - ffmpeg: ayuda a leer videos en entornos slim
# - ca-certificates/curl/git: útil para descargas y debug
# - libportaudio2: mediapipe importa sounddevice al cargarse
# - libegl1/libgles2: mediapipe enlaza EGL/GLES aunque corra en CPU
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    curl \
    ca-certificates \
    git \
    libportaudio2 \
    libegl1 \
    libgles2 \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instala deps primero (mejor cache)
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip && pip install -r requirements.txt

# Modelo de ASR horneado en la imagen: sin esto, el primer request tras cada
# reinicio se descargaria ~250MB desde Hugging Face.
ARG ASR_MODEL_SIZE=small
ENV ASR_MODEL_SIZE=${ASR_MODEL_SIZE} \
    HF_HOME=/app/.cache/huggingface
RUN mkdir -p /app/.cache/huggingface && \
    python -c "from faster_whisper import WhisperModel; WhisperModel('${ASR_MODEL_SIZE}', device='cpu', compute_type='int8'); print('Whisper model ready')"

# Modelos de rostro de OpenCV Zoo (YuNet MIT + SFace Apache 2.0), con checksum
# para que una descarga corrupta o un modelo cambiado rompa el build.
ARG ZOO=https://github.com/opencv/opencv_zoo/raw/main/models
RUN mkdir -p /app/models && cd /app/models && \
    curl -fsSL -o face_detection_yunet_2023mar.onnx  ${ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx && \
    curl -fsSL -o face_recognition_sface_2021dec.onnx ${ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx && \
    echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  face_detection_yunet_2023mar.onnx" | sha256sum -c - && \
    echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  face_recognition_sface_2021dec.onnx" | sha256sum -c -

# MediaPipe Face Landmarker (Apache 2.0) para la sincronia labios-voz.
RUN cd /app/models && \
    curl -fsSL -o face_landmarker.task https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task && \
    echo "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff  face_landmarker.task" | sha256sum -c -

# Copia tu código
COPY app /app/app

# Hugging Face Spaces suele exponer 7860
EXPOSE 7860

# Respeta PORT si HF lo define
CMD ["bash", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860}"]

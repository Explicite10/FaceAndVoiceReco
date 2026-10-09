# FaceAndVoiceReco

API de verificación de identidad contra **una foto de identificación (INE)**. Tiene dos modos:

- **`POST /api/verify-identity`**: con **un video corto** de la persona leyendo una frase en voz alta. Es el más completo.
- **`POST /api/verify-photo`**: con **una foto** de la persona. Compara las caras y valida la calidad de la foto.

El endpoint de video valida tres cosas en una sola llamada:

| Análisis | Pregunta que responde | Modelos |
|---|---|---|
| **Rostro** | ¿La cara del video es la misma que la de la identificación? | YuNet (detección) + SFace (reconocimiento) |
| **Frase hablada** | ¿La persona dijo la frase esperada? | faster-whisper (transcripción) + VAD de Silero |
| **Prueba de vida** | ¿Es una persona real frente a la cámara y no una pantalla, foto o audio doblado? | MiniFASNet (anti-spoofing) + MediaPipe Face Landmarker (sincronía labios-voz) |

> **Nota:** el análisis de voz verifica *qué* se dijo (reconocimiento del habla), no *quién* lo dijo. No hay biometría de voz.

La solicitud se aprueba solo si **ninguna** validación falla.

---

## Cómo funciona

```
id_image ─────────────────────────────┐
                                      ▼
video ──► guardado temporal ──► 1. Voz      audio 16 kHz → VAD → Whisper → WER + similitud de texto
                              ├► 2. Rostro  3 tramos → cara más nítida de cada uno → similitud coseno vs. INE
                              └► 3. Vida    anti-spoofing por tramo + movimiento de boca vs. energía del audio
                                      │
                                      ▼
                         reporte con checks, detalles y decisión final
```

- **Rostro.** El video se divide en 3 tramos de igual duración y en cada uno se elige la cara más nítida. Es *match* solo si **los 3 tramos** superan el umbral; con un simple promedio, un tramo con otra persona pasaría desapercibido. Si aparece más de una cara en varios cuadros, se rechaza.
- **Frase.** Antes de transcribir se descartan audios muy cortos, muy largos o con poca voz (en esos casos Whisper tiende a inventar texto). El puntaje combina WER (70 %) y similitud de caracteres (30 %), y respeta el orden de las palabras.
- **Prueba de vida.** MiniFASNet estima la probabilidad de que la cara sea real en cada tramo. Además se mide la apertura de la boca cuadro por cuadro y se correlaciona con la energía del audio.
- Los análisis corren **en secuencia** (todos usan mucha CPU) y están **aislados**: si falla uno, los demás se reportan de todos modos.

---

## Estructura

```
app/
├── main.py                       # FastAPI + carga de modelos al arrancar
├── core/config.py                # umbrales y variables de entorno
├── api/routes/identity.py        # POST /api/verify-identity
├── schemas/identity.py           # contrato de respuesta (Pydantic)
├── services/
│   ├── identity_verification.py  # orquesta los tres análisis
│   ├── face_engine.py            # YuNet + SFace
│   ├── video_face.py             # mejor cara por tramo del video
│   ├── face_service.py           # comparación contra la identificación
│   ├── photo_verification.py     # foto contra identificación
│   ├── image_quality.py          # tamaño, nitidez y brillo de la cara
│   ├── audio_extract.py          # audio del video a 16 kHz mono
│   ├── asr_engine.py             # modelo Whisper
│   ├── audio_liveness.py         # filtros de audio + transcripción
│   ├── phrase_match.py           # puntaje de la frase
│   ├── antispoof.py              # MiniFASNet (ONNX)
│   ├── lip_sync.py               # sincronía labios-voz
│   ├── liveness.py               # prueba de vida
│   └── report.py                 # arma la respuesta pública
├── assets/antispoof/             # modelos MiniFASNet incluidos en el repo
└── templates/index.html          # página de prueba con cámara
```

---

## Ejecutar

### Con Docker (recomendado)

```bash
docker build -t face-voice-reco .
docker run -d --name face-voice-reco --restart unless-stopped -p 7860:7860 --env-file .env face-voice-reco
```

El archivo `.env` va junto al proyecto en el servidor (no se sube a git ni entra a la imagen) y tiene la configuración de producción, por ejemplo:

```
API_KEYS=crm:LLAVE_DEL_CRM,portal:LLAVE_DEL_PORTAL
CORS_ORIGINS=https://portal.tuempresa.com
RATE_LIMIT_REQUESTS=20
```

La imagen ya trae todos los modelos, así que arranca sin descargar nada. Corre con un usuario sin privilegios y tiene un `HEALTHCHECK` contra `GET /health`: `docker ps` muestra `healthy` cuando los modelos ya cargaron.

Se construye para `linux/amd64` porque MediaPipe no publica paquete para Linux ARM; en Mac con Apple Silicon funciona mediante emulación, aunque más lento.

Para cambiar la configuración, pasa variables con `-e` (ver [Configuración](#configuración)). Por ejemplo, con un modelo de Whisper más grande horneado en la imagen:

```bash
docker build --build-arg ASR_MODEL_SIZE=medium -t face-voice-reco .
```

**Límites que aplica la API:**

- Video: 50 MB. Imágenes: 10 MB y 50 megapixeles; formatos JPEG, PNG y WEBP.
- 10 verificaciones por minuto por proyecto (API key), sumando ambos endpoints; sin llaves configuradas, por IP. Al pasarse responde `429` con `Retry-After`.

Si la API se publica detrás de un proxy o balanceador, define `FORWARDED_ALLOW_IPS` con la IP de ese proxy. Si no, el rate limit ve a todos los usuarios con la IP del proxy y los cuenta como uno solo. El conteo es en memoria, así que la API debe correr con un solo worker de uvicorn (como está configurada).

### Local

Requiere Python 3.12.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

En el primer arranque se descargan los modelos de rostro y de MediaPipe a `models/` (con verificación SHA-256), y el modelo de Whisper a la caché de Hugging Face.

Abre `http://localhost:8000` para usar la página de prueba con la cámara, o `http://localhost:8000/docs` para la documentación interactiva.

---

## Acceso: API keys y CORS

Cada proyecto que consume la API tiene su propia llave y la manda en el header `X-API-Key`:

```bash
curl -X POST https://tu-servidor/api/verify-photo \
  -H "X-API-Key: LLAVE_DEL_PROYECTO" \
  -F "id_image=@ine.jpg" \
  -F "photo=@selfie.jpg"
```

- Las llaves se configuran en `API_KEYS` como `proyecto:llave` separados por coma. Para generar una llave:
  ```bash
  python3 -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
- Sin llave o con una llave incorrecta, la API responde `401`. `GET /health` no pide llave.
- Para dar de baja un proyecto, quita su llave de `API_KEYS` y reinicia el contenedor; los demás no se enteran.
- El rate limit se cuenta por proyecto, así que un proyecto con mucho tráfico no le quita cupo a los demás.
- Si `API_KEYS` está vacío, la API **no pide autenticación**. Es solo para desarrollo, y al arrancar se advierte en el log. Si `API_KEYS` está mal escrito, el contenedor no arranca en lugar de quedar abierto.
- En Swagger (`/docs`), el botón **Authorize** permite poner la llave para probar.

**CORS** solo hace falta si el navegador de los usuarios llama directo a la API. Si llama el backend de cada proyecto, no se configura. Los orígenes permitidos van en `CORS_ORIGINS`, separados por coma. Ojo: una llave usada desde un frontend queda visible para cualquiera que abra las herramientas del navegador, así que lo recomendable es que las llamadas pasen por el backend de cada proyecto.

---

## API

### `POST /api/verify-identity`

`multipart/form-data`:

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `id_image` | archivo de imagen | sí | Foto de la identificación |
| `video` | `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.webm` | sí | Video de 4 a 20 s, máximo 50 MB |
| `expected_phrase` | texto | no | Frase esperada; si se omite, usa la configurada |
| `include_images` | bool | no | Agrega los recortes de cara en base64 (~70 KB) |

```bash
curl -X POST http://localhost:8000/api/verify-identity \
  -F "id_image=@ine.jpg" \
  -F "video=@video.mp4"
```

### Respuesta (resumida)

```json
{
  "request_id": "…",
  "processing_time_ms": 0,
  "result": {
    "decision": "approved | rejected",
    "approved": true,
    "failed_checks": [],
    "summary": "…"
  },
  "checks": {
    "face":     { "id_face_detected": {}, "video_face_detected": {}, "face_in_all_segments": {}, "face_match": {}, "single_face": {} },
    "voice":    { "audio_present": {}, "audio_duration": {}, "speech_detected": {}, "transcription_confidence": {}, "phrase_match": {} },
    "liveness": { "anti_spoofing": {}, "face_tracking": {}, "lip_movement": {}, "lip_sync": {} }
  },
  "details": { "face": {}, "voice": {}, "liveness": {} },
  "errors": [],
  "artifacts": null,
  "meta": { "api_version": "1.0.0", "models": {} }
}
```

Cada check tiene la forma `{ "status": "passed | failed | not_evaluated", "code": "…", "message": "…" }`. El campo `code` solo viene cuando el check falla y es estable (por ejemplo `face_mismatch`, `spoof_detected` o `phrase_mismatch`), así que el cliente puede depender de él en lugar del mensaje. `not_evaluated` significa que falló un paso previo.

### `POST /api/verify-photo`

Compara una foto de la persona contra la identificación. `multipart/form-data`:

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `id_image` | archivo de imagen | sí | Foto de la identificación |
| `photo` | archivo de imagen | sí | Foto de la persona (selfie) |
| `include_images` | bool | no | Agrega los recortes de cara en base64 |

```bash
curl -X POST http://localhost:8000/api/verify-photo \
  -F "id_image=@ine.jpg" \
  -F "photo=@selfie.jpg"
```

Validaciones (`checks`):

| Grupo | Check | Falla con |
|---|---|---|
| `face` | `id_face_detected` | `id_face_not_found` |
| `face` | `photo_face_detected` | `photo_face_not_found` |
| `face` | `single_face` | `multiple_faces_detected` |
| `face` | `face_match` | `face_mismatch` |
| `quality` | `face_size` | `face_too_small` (lado menor de la cara < 112 px) |
| `quality` | `sharpness` | `image_blurry` |
| `quality` | `brightness` | `image_too_dark` / `image_overexposed` |
| `liveness` | `anti_spoofing` | `spoof_detected` |

La calidad solo se valida en la foto de la persona. La de la identificación se reporta en `details.id_face_quality`, pero no se valida, porque la foto de la INE suele ser chica. La respuesta tiene la misma forma general que la del video (`result`, `checks`, `details`, `artifacts`, `meta`), y en `details.similarity` vienen `score`, `percent` y `threshold_score`.

Este modo es **más débil** que el de video: no hay sincronía de labios ni frase hablada, solo anti-spoofing sobre una imagen.

**Sobre los porcentajes de similitud facial:** `similarity_percent` es una curva sigmoide centrada en el umbral (≥ 50 % equivale a *match*) pensada para mostrarse en pantalla. **No es una probabilidad.** El valor real es `similarity_score` (similitud coseno).

---

## Configuración

Variables de entorno:

| Variable | Default | Descripción |
|---|---|---|
| `EXPECTED_PHRASE` | `yo confirmo mi identidad y autorizo la verificacion de mi cuenta el dia de hoy` | Frase por defecto |
| `ASR_MODEL_SIZE` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `ASR_DEVICE` | `cpu` | `cuda` si hay GPU |
| `ASR_COMPUTE_TYPE` | `int8` | `float16` con GPU |
| `ASR_CPU_THREADS` | `4` | Hilos de CPU para Whisper |
| `ASR_MAX_CONCURRENCY` | `1` | Transcripciones simultáneas (≈ núcleos / hilos) |
| `FACE_MODELS_DIR` | `models/` | Carpeta de los modelos descargados |
| `API_KEYS` | vacío | `proyecto:llave` separados por coma; vacío = sin autenticación (solo desarrollo) |
| `CORS_ORIGINS` | vacío | Orígenes de navegador permitidos, separados por coma; vacío = sin CORS |
| `RATE_LIMIT_REQUESTS` | `10` | Verificaciones permitidas por proyecto (o por IP sin llaves) en la ventana; `0` lo desactiva |
| `RATE_LIMIT_WINDOW_SEC` | `60` | Duración de la ventana del rate limit |
| `MAX_VIDEO_MB` | `50` | Tamaño máximo del video |
| `MAX_IMAGE_MB` | `10` | Tamaño máximo de cada imagen |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs del proxy de las que se acepta `X-Forwarded-For` |

Los umbrales (similitud facial, anti-spoofing, sincronía de labios, puntaje de la frase, duraciones) están en [`app/core/config.py`](app/core/config.py).

---

## Limitaciones conocidas

- **La frase no es un reto real.** El cliente puede enviar `expected_phrase`, y la frase por defecto es fija. Alguien con una grabación de la víctima diciendo la frase podría pasar la validación de voz. Para producción, el servidor debería generar una frase aleatoria por sesión y validarla contra esa sesión.
- **Umbrales sin calibrar.** El de SFace (`0.363`) es el publicado por OpenCV, y los de sincronía de labios y calidad de foto son provisionales. Hay que ajustarlos con pares reales de INE y video o foto.
- No hay pruebas automatizadas.

---

## Licencias de los modelos

Todos permiten uso comercial:

- **YuNet**: MIT · **SFace**: Apache 2.0 ([OpenCV Zoo](https://github.com/opencv/opencv_zoo))
- **MiniFASNet**: Apache 2.0 ([Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing)); ver [`app/assets/antispoof/NOTICE`](app/assets/antispoof/NOTICE)
- **MediaPipe Face Landmarker**: Apache 2.0
- **Whisper**: MIT

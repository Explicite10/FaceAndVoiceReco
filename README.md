# FaceAndVoiceReco

API de verificación de identidad contra **una foto de identificación (INE)**. Tiene tres formas de uso:

- **Sesiones para proyectos autorizados (recomendado).** El backend del proyecto crea una sesión con la foto de la INE, manda al usuario a una pantalla mínima de prueba de vida y recibe el resultado en JSON por un webhook firmado. Ver [Integración para proyectos](#integración-para-proyectos-sesiones).
- **`POST /api/verify-identity`**: con **un video corto** en el que la persona hace unos gestos al azar y lee una frase en voz alta. Es el más completo. Antes se pide un reto en `POST /api/challenge`.
- **`POST /api/verify-photo`**: con **una foto** de la persona. Compara las caras y valida la calidad de la foto.

El endpoint de video valida cuatro cosas en una sola llamada:

| Análisis | Pregunta que responde | Modelos |
|---|---|---|
| **Rostro** | ¿La cara del video es la misma que la de la identificación? | YuNet (detección) + SFace (reconocimiento) |
| **Frase hablada** | ¿La persona dijo la frase esperada? | faster-whisper (transcripción) + VAD de Silero |
| **Prueba de vida** | ¿Es una persona real frente a la cámara y no una pantalla, foto o audio doblado? | MiniFASNet (anti-spoofing) + MediaPipe Face Landmarker (sincronía labios-voz) |
| **Gestos del reto** | ¿Hizo, en orden, los gestos que pidió el servidor (girar la cabeza, parpadear, alzar una mano)? | MediaPipe Face Landmarker + Hand Landmarker |

> **Nota:** el análisis de voz verifica *qué* se dijo (reconocimiento del habla), no *quién* lo dijo. No hay biometría de voz.

La solicitud se aprueba solo si **ninguna** validación falla.

---

## Cómo funciona

```
POST /api/challenge ──► challenge_id + 3 gestos en orden aleatorio + frase aleatoria (un solo uso, 3 min)
        │
        ▼  el navegador guía los gestos en vivo (MediaPipe web) y graba gestos + frase en un solo video
id_image ─────────────────────────────┐
                                      ▼
video + challenge_id ──► guardado temporal ──► 1. Voz      audio 16 kHz → VAD → Whisper → WER contra la frase del reto
                                             ├► 2. Rostro  3 tramos → cara más nítida de cada uno → similitud coseno vs. INE
                                             ├► 3. Vida    anti-spoofing por tramo + movimiento de boca vs. energía del audio
                                             └► 4. Gestos  giro, parpadeo y mano detectados de nuevo en el video, en orden
                                      │
                                      ▼
                         reporte con checks, detalles y decisión final
```

- **Rostro.** El video se divide en 3 tramos de igual duración y en cada uno se elige la cara más nítida. Es *match* solo si **los 3 tramos** superan el umbral; con un simple promedio, un tramo con otra persona pasaría desapercibido. Si aparece más de una cara en varios cuadros, se rechaza.
- **Frase.** Antes de transcribir se descartan audios muy cortos, muy largos o con poca voz (en esos casos Whisper tiende a inventar texto). El puntaje combina WER (70 %) y similitud de caracteres (30 %), y respeta el orden de las palabras.
- **Prueba de vida.** MiniFASNet estima la probabilidad de que la cara sea real en cada tramo. Además se mide la apertura de la boca cuadro por cuadro y se correlaciona con la energía del audio.
- **Reto.** El servidor sortea la frase (3 palabras de una lista de 40) y el orden y lado de los gestos: girar la cabeza a la izquierda o derecha, parpadear y alzar la mano izquierda o derecha. El cliente solo devuelve el `challenge_id`, así que no puede elegir la frase ni reusar un video grabado para otro reto. El navegador detecta los gestos en vivo para guiar al usuario, pero eso no se confía: el servidor los vuelve a buscar en el video subido y revisa el orden. Izquierda y derecha son las del usuario.
- Los análisis corren **en secuencia** (todos usan mucha CPU) y están **aislados**: si falla uno, los demás se reportan de todos modos.

---

## Estructura

```
app/
├── main.py                       # FastAPI + carga de modelos al arrancar
├── db.py                         # PostgreSQL: proyectos y resumen de sesiones
├── cli.py                        # alta y baja de proyectos (python -m app.cli)
├── core/config.py                # umbrales y variables de entorno
├── api/routes/identity.py        # POST /api/challenge, /api/verify-identity y /api/verify-photo
├── schemas/identity.py           # contrato de respuesta (Pydantic)
├── services/
│   ├── sesiones.py               # sesiones para proyectos: API, captura y ciclo de vida
│   ├── proyectos.py              # proyectos autorizados (API key en hash, webhook, dominios)
│   ├── entrega.py                # webhook JSON firmado al proyecto
│   ├── identity_verification.py  # orquesta los análisis
│   ├── challenge.py              # retos de un solo uso (frase y gestos)
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
│   ├── face_track.py             # una pasada de MediaPipe (cara y manos)
│   ├── lip_sync.py               # sincronía labios-voz
│   ├── gestures.py               # gestos del reto
│   ├── liveness.py               # prueba de vida
│   └── report.py                 # arma la respuesta pública
├── assets/antispoof/             # modelos MiniFASNet incluidos en el repo
├── templates/captura.html        # pantalla mínima de prueba de vida (sesiones)
├── templates/index.html          # página de prueba completa (solo con DEMO=true)
└── static/captura.{js,css}       # lógica y estilos de la captura
ejemplos/proyecto_demo.py         # proyecto cliente de ejemplo (crear sesión + webhook)
tests/test_sesiones.py            # pruebas del flujo de sesiones
docker-compose.yml                # API + PostgreSQL con arranque automático
.env.example                      # plantilla de configuración del servidor
```

---

## Ejecutar

### Con Docker en un servidor (recomendado)

En el servidor (Linux amd64 con Docker y el plugin `docker compose`):

```bash
git clone <repo> && cd <repo>
cp .env.example .env        # llenar POSTGRES_PASSWORD (obligatoria) y revisar PUBLIC_URL
docker compose up -d --build
```

Levanta dos contenedores: `face-voice-reco` (la API) y `face-voice-reco-db` (PostgreSQL, con sus datos en el volumen `pgdata`). Después se da de alta cada proyecto autorizado:

```bash
docker compose exec api python -m app.cli crear "Créditos" \
    --webhook https://api.creditos.com/webhooks/vida --dominios creditos.com
```

Para que arranque solo cuando se prende el servidor, el contenedor ya tiene `restart: always` en [`docker-compose.yml`](docker-compose.yml). Solo falta que Docker arranque con el sistema (una vez):

```bash
sudo systemctl enable docker
```

Para revisar que quedó bien:

```bash
docker compose ps               # debe decir "healthy"
curl http://localhost:7860/health
docker compose logs -f api
```

Para actualizar después de un `git pull`, se repite `docker compose up -d --build`. Las sesiones a medias se pierden con el reinicio (la INE solo vive en memoria): quedan como `expiradas` y se avisa a su proyecto.

Respaldo de la base:

```bash
docker compose exec db pg_dump -U vida vida > respaldo-$(date +%F).sql
```

El archivo `.env` va junto al proyecto en el servidor (no se sube a git ni entra a la imagen). [`.env.example`](.env.example) tiene todas las variables explicadas. Por ejemplo:

```
PUBLIC_URL=https://prueba-de-vida.buzzword.com.mx
POSTGRES_PASSWORD=<generada con secrets.token_urlsafe>
DEMO=false
```

La imagen ya trae todos los modelos, así que arranca sin descargar nada. Corre con un usuario sin privilegios y tiene un `HEALTHCHECK` contra `GET /health`.

**La cámara del navegador solo funciona con HTTPS** (o en `localhost`). Si la página de prueba se abre como `http://IP:7860`, no puede grabar. La URL pública debe ser `https://`, normalmente con el certificado en el proxy o balanceador de TI que apunta a `http://<servidor>:7860`. Ese proxy debe aceptar cuerpos de unos 64 MB y no cortar respuestas antes de unos 2 minutos.

Se construye para `linux/amd64` porque MediaPipe no publica paquete para Linux ARM; en Mac con Apple Silicon funciona mediante emulación, aunque más lento. Sin `docker compose`, el equivalente es:

```bash
docker build -t face-voice-reco .
docker run -d --name face-voice-reco --restart always -p 7860:7860 --env-file .env face-voice-reco
```

Para incluir un modelo de Whisper más grande en la imagen:

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
DEMO=true uvicorn app.main:app --reload
```

En el primer arranque se descargan los modelos de rostro y de MediaPipe a `models/` (con verificación SHA-256), y el modelo de Whisper a la caché de Hugging Face.

Abre `http://localhost:8000` para usar la página de prueba con la cámara, o `http://localhost:8000/docs` para la documentación interactiva. Sin `DATABASE_URL` todo funciona menos las sesiones. Para tenerlas en local, con un PostgreSQL cualquiera:

```bash
DATABASE_URL=postgresql://usuario:clave@localhost:5432/vida ALLOW_HTTP_URLS=true uvicorn app.main:app --reload
```

Pruebas (necesitan un PostgreSQL de pruebas; ver el encabezado de [`tests/test_sesiones.py`](tests/test_sesiones.py)):

```bash
pip install -r requirements-dev.txt
TEST_DATABASE_URL=postgresql://postgres:test@127.0.0.1:55432/postgres pytest -q
```

---

## Acceso: proyectos autorizados

Solo los proyectos dados de alta pueden usar el servicio. Cada uno tiene su API key (en la base solo queda su hash), el secreto con el que se firman sus webhooks y los dominios a los que puede regresar al usuario, mandar resultados e incrustar la captura.

```bash
docker compose exec api python -m app.cli crear "Créditos" --webhook https://api.creditos.com/webhooks/vida --dominios creditos.com
docker compose exec api python -m app.cli listar
docker compose exec api python -m app.cli rotar-key prj_...        # la llave anterior deja de servir
docker compose exec api python -m app.cli desactivar prj_...       # quita el acceso (activar lo devuelve)
docker compose exec api python -m app.cli webhook prj_... https://nueva-url
docker compose exec api python -m app.cli dominios prj_... creditos.com,app.creditos.com
```

`crear` muestra la `api_key` **una sola vez**, junto con el `webhook_secreto`; ambos se guardan en el backend del proyecto. Sin `--dominios`, el proyecto puede usar cualquier dominio (solo para pruebas).

La misma llave sirve para la API directa (`/api/challenge`, `/api/verify-identity`, `/api/verify-photo`) en el header `X-API-Key`. El rate limit se cuenta por proyecto. También se aceptan llaves fijas en `API_KEYS` (`nombre:llave`), útiles para pruebas internas.

**`DEMO`** controla el modo de pruebas y está apagado por defecto. Con `DEMO=true` existe la página completa en `/` y, si no hay `API_KEYS`, la API directa no pide llave; es solo para desarrollo. Apagado, `/` responde 404 y todo pide la llave de un proyecto.

**CORS** solo hace falta si un frontend llama directo a la API directa desde el navegador. Las sesiones no lo necesitan: el backend del proyecto llama a la API, y la captura vive en este mismo dominio. Una llave usada desde un frontend queda visible para cualquiera, así que lo recomendable es que las llamadas pasen por el backend de cada proyecto.

---

## Integración para proyectos (sesiones)

Cada proyecto **no** copia la cámara ni el análisis: pide una sesión, manda al usuario a hacer la prueba y recibe el resultado en JSON. Es el mismo esquema que el servicio de huellas.

> Para los equipos de cada proyecto hay una guía independiente del lenguaje en [`docs/INTEGRACION.md`](docs/INTEGRACION.md): qué mandar, qué reciben, cómo verificar la firma (con un ejemplo para comprobarla) y una lista de verificación.
>
> Para quien administra el servidor: [`docs/SERVIDOR.md`](docs/SERVIDOR.md) (despliegue, proxy HTTPS, alta de proyectos, respaldos) y [`scripts/verificar-produccion.sh`](scripts/verificar-produccion.sh) para comprobar que sin llave no se entra.

```
Backend del proyecto                    Prueba de vida                               Usuario
1. POST /api/v1/sesiones (INE)  ─────►  revisa la cara de la INE ─► { id, url_captura }
2. redirige a url_captura  ─────────────────────────────────────────────────────────►  gestos + frase
                                        3. analiza; si no pasa, el usuario reintenta
4. POST JSON firmado al webhook ◄─────  evento + sesión + reporte completo
   lo guarda y responde 200 ──────────► olvida la INE
5.                                      "¡Listo!" y regresa a url_retorno ──────────►  sigue su trámite
```

**Qué se guarda:** en PostgreSQL, solo el resumen de cada sesión (proyecto, referencia, estado, aprobada, intentos, checks fallidos, si se entregó y cuándo) y de cada intento. **Nunca** el video, la INE ni datos de la cara o la voz. La INE se convierte en un vector de 128 números al crear la sesión, se descarta la imagen, y el vector vive solo en memoria hasta que termina la sesión. El video se borra en cuanto se analiza. Si el proyecto crea la sesión con `incluir_video`, `incluir_fotogramas` o `incluir_rostro_ine`, el webhook trae ligas temporales (`EVIDENCE_MINUTES`, 60 por defecto) al video en MP4, 3 fotogramas de frente y la cara de la INE del intento final; el proyecto los descarga con su `X-API-Key`. Viven en un directorio temporal del contenedor y se borran al vencer, con `DELETE /api/v1/sesiones/{id}/evidencia` o al reiniciar.

### 1. Crear una sesión (desde el backend, nunca desde el navegador)

`multipart/form-data`, con la INE como archivo binario (no base64):

```bash
curl -X POST https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones \
  -H "X-API-Key: pv_..." \
  -F "ine_imagen=@ine.jpg" \
  -F "frase=acepto los términos y condiciones de mi crédito" \
  -F "referencia_externa=cliente-123" \
  -F "url_retorno=https://creditos.com/registro/paso-4" \
  -F 'metadata={"tramite": "alta"}'
```

| Campo | Requerido | Descripción |
|---|---|---|
| `ine_imagen` | sí | Archivo con la foto de la INE (JPEG, PNG o WEBP; máx. 10 MB). El formato se revisa por el contenido, así que el `Content-Type` del archivo puede ser `application/octet-stream` |
| `frase` | sí | Lo que el usuario lee en voz alta después de los gestos, de 4 a 50 palabras. Conviene que cambie en cada sesión (por ejemplo, con el folio del trámite o el nombre) y que los números vayan en palabras o sean de hasta tres cifras: Whisper escribe "4,521" y eso se compara mal |
| `referencia_externa` | no | Tu identificador (cliente, trámite). Vuelve en el webhook |
| `url_retorno` | no | A dónde regresa el usuario. Se le agregan `?sesion_id=…&estado=…&aprobada=1\|0` |
| `webhook_url` | no | Dónde se entrega el resultado; si no, la del proyecto. Debe existir en alguno de los dos |
| `max_intentos` | no | 1 a 5; por defecto 3 |
| `expira_minutos` | no | 5 a 1440; por defecto 30 |
| `umbral_rostro` | no | % mínimo de similitud con la INE; por defecto 70 |
| `metadata` | no | Objeto JSON en texto (máx. 4 KB) que vuelve en el webhook |
| `logo` | no | Logo para la pantalla del usuario (PNG, JPEG o WEBP; máx. 1 MB) |
| `color_primario` / `color_secundario` | no | Colores hexadecimales (`#1B56D6`). Sin ellos, la pantalla usa los colores genéricos |

Si no se detecta una cara en la INE responde `422` de inmediato, sin mandar al usuario a grabar. Respuesta (`201`):

```json
{ "id": "ses_…", "estado": "pendiente", "url_captura": "https://…/captura/<token>",
  "intentos": 0, "max_intentos": 3, "expira": "2026-10-14T18:35:01+00:00", … }
```

### 2. Mandar al usuario a la captura

- **Redirección** (recomendado): `location.href = url_captura`. Funciona en computadora y celular. Al terminar vuelve a `url_retorno`.
- **Iframe** (el dominio del proyecto debe estar en `--dominios`):
  ```html
  <iframe src="URL_CAPTURA" allow="camera; microphone" style="width:100%;height:100vh;border:0"></iframe>
  <script>
    addEventListener("message", (e) => {
      if (e.origin === "https://prueba-de-vida.buzzword.com.mx" && e.data?.tipo === "prueba-vida:finalizada") {
        // e.data = { sesion_id, estado, aprobada }; el resultado completo ya llegó a tu webhook
      }
    });
  </script>
  ```
- **App móvil:** abrir `url_captura` en un WebView con permiso de cámara y micrófono.

En cada intento el servidor sortea tres gestos (girar la cabeza, parpadear y alzar una mano, en orden y lado al azar); la frase es siempre la que mandó el proyecto. El usuario solo ve: una pantalla de inicio, la cámara con un paso a la vez (3 gestos y la frase), "Verificando…" y el resultado. Si un intento no pasa, ve todos los motivos con cómo corregirlos ("No se detectó el parpadeo. Cierra bien los ojos un momento.") y puede reintentar con un reto nuevo hasta `max_intentos`.

### 3. Recibir el resultado (webhook)

Un `POST` con `Content-Type: application/json` a `webhook_url`, en cuanto la sesión se aprueba o se agotan los intentos:

```json
{
  "id": "evt_…", "evento": "sesion.completada", "creado": "2026-10-14T18:08:26+00:00",
  "datos": {
    "sesion": { "id": "ses_…", "referencia_externa": "cliente-123", "frase": "acepto los términos…",
                "estado": "completada", "aprobada": true, "intentos": 2, "max_intentos": 3,
                "checks_fallidos": [], "metadata": {"tramite": "alta"}, … },
    "resumen": {
      "aprobada": true,
      "checks_fallidos": [],
      "rostro": { "coincide": true, "similitud_porcentaje": 88.4, "similitud_minima_porcentaje": 86.2,
                  "umbral_porcentaje": 70.0, "una_sola_persona": true },
      "frase":  { "coincide": true, "coincidencia_porcentaje": 96.5, "umbral_porcentaje": 85.0,
                  "esperada": "acepto los términos…", "transcripcion": "acepto los términos…" },
      "vida":   { "persona_real": true, "probabilidad_real_porcentaje": 93.1, "umbral_porcentaje": 50.0,
                  "labios_sincronizados": true },
      "gestos": { "completos": true, "en_orden": true,
                  "detalle": [ { "gesto": "turn_left", "detectado": true, "segundo": 1.4 }, … ] }
    },
    "resultado": { "result": { "approved": true, "failed_checks": [], "summary": "…" },
                   "checks": { "face": {…}, "voice": {…}, "liveness": {…}, "challenge": {…} },
                   "details": { … }, "meta": { … } },
    "intentos": [ { "numero": 1, "aprobado": false, "checks_fallidos": ["challenge.blink"], … },
                  { "numero": 2, "aprobado": true, "checks_fallidos": [], … } ]
  }
}
```

`resumen` es lo que normalmente se guarda: la decisión, y por cada validación una bandera y su porcentaje. Las banderas son `true` (pasó), `false` (falló) o `null` (no se pudo evaluar porque falló un paso previo; por ejemplo, sin rostro en el video no hay similitud). La sesión se aprueba solo si todas pasan. `resultado` es el mismo reporte que devuelve `POST /api/verify-identity` (ver [Respuesta](#respuesta-resumida)), del último intento. Otros eventos: `sesion.expirada` y `sesion.cancelada`, solo con `sesion`.

Cabeceras: `X-Vida-Evento`, `X-Vida-Entrega` (id único, para ignorar duplicados) y `X-Vida-Firma: t=<unix>,v1=<HMAC-SHA256(webhook_secreto, "<t>." + cuerpo_crudo)>`.

**Responde 2xx solo después de guardar.** Es la confirmación: el usuario ve "¡Listo!" y el servicio olvida la INE. Si respondes error o no contestas en 20 s, se reintenta 2 veces; si sigue fallando, el usuario ve "No se pudo registrar el resultado" con un botón para reintentar el envío (mientras no venza la sesión).

Verificar la firma, Python (FastAPI):

```python
import hashlib, hmac, json, time

@app.post("/webhooks/vida")
async def webhook(request: Request):
    cuerpo = await request.body()                     # cuerpo crudo, tal cual llegó
    partes = dict(p.split("=", 1) for p in request.headers["X-Vida-Firma"].split(","))
    esperado = hmac.new(SECRETO.encode(), f"{partes['t']}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    if abs(time.time() - int(partes["t"])) > 300 or not hmac.compare_digest(esperado, partes["v1"]):
        raise HTTPException(401)
    evento = json.loads(cuerpo)
    guardar_en_bd(evento["datos"])
    return {"ok": True}                               # 200 = "ya lo guardé"
```

Node / Nuxt (`server/api/webhooks/vida.post.ts`):

```ts
import { createHmac, timingSafeEqual } from "node:crypto";
export default defineEventHandler(async (event) => {
  const cuerpo = (await readRawBody(event, false)) as Buffer;
  const partes = Object.fromEntries(getHeader(event, "x-vida-firma")!.split(",").map((p) => p.split("=")));
  const esperado = createHmac("sha256", process.env.VIDA_WEBHOOK_SECRETO!).update(`${partes.t}.`).update(cuerpo).digest("hex");
  const a = Buffer.from(esperado), b = Buffer.from(partes.v1 ?? "");
  if (Math.abs(Date.now() / 1000 - Number(partes.t)) > 300 || a.length !== b.length || !timingSafeEqual(a, b))
    throw createError({ statusCode: 401 });
  const evento = JSON.parse(cuerpo.toString());
  // guardar evento.datos en tu BD
  return { ok: true };
});
```

[`ejemplos/proyecto_demo.py`](ejemplos/proyecto_demo.py) es un proyecto cliente completo (subir INE, crear sesión, verificar la firma y mostrar el resultado), útil para probar todo con la cámara.

### 4. Otras rutas para el backend del proyecto

| Método | Ruta | Para qué |
|---|---|---|
| GET | `/api/v1/sesiones/{id}` | Estado de la sesión y sus intentos (sin reporte ni biometría) |
| GET | `/api/v1/sesiones?estado=&limite=` | Sesiones del proyecto, de la más reciente a la más vieja |
| POST | `/api/v1/sesiones/{id}/cancelar` | Cancelar una sesión activa |

Todas llevan `X-API-Key` y solo ven las sesiones de su proyecto. Estados: `pendiente`, `procesando`, `entregando`, `completada`, `error_entrega`, `expirada`, `cancelada`.

---

## API

### `POST /api/challenge`

Pide un reto para un video. No lleva cuerpo.

```bash
curl -X POST http://localhost:8000/api/challenge -H "X-API-Key: LLAVE_DEL_PROYECTO"
```

```json
{
  "challenge_id": "…",
  "phrase": "confirmo mi identidad con las palabras tortuga, cometa y jardín",
  "gestures": [
    { "gesture": "blink", "instruction": "Parpadea despacio" },
    { "gesture": "raise_right_hand", "instruction": "Alza tu mano derecha junto a la cara" },
    { "gesture": "turn_left", "instruction": "Gira la cabeza a tu izquierda" }
  ],
  "expires_in_sec": 180
}
```

En el video, la persona hace los gestos **en ese orden** y después lee la frase. El reto vence en `expires_in_sec`, sirve para un solo video (aunque la verificación falle) y solo lo puede usar el proyecto que lo pidió. Tiene su propio rate limit (`CHALLENGE_RATE_LIMIT_REQUESTS`). Gestos posibles: `turn_left`, `turn_right`, `blink`, `raise_left_hand`, `raise_right_hand`; izquierda y derecha son las del usuario.

### `POST /api/verify-identity`

`multipart/form-data`:

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `id_image` | archivo de imagen | sí | Foto de la identificación |
| `video` | `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.webm` | sí | Video de 4 a 60 s, máximo 50 MB |
| `challenge_id` | texto | sí* | El de `POST /api/challenge`. *Opcional solo con `REQUIRE_CHALLENGE=false` |
| `expected_phrase` | texto | no | Solo sin `challenge_id`; con reto se ignora |
| `match_threshold_percent` | número 1–100 | no | % mínimo de similitud facial en cada tramo; default 70 |

```bash
curl -X POST http://localhost:8000/api/verify-identity \
  -F "id_image=@ine.jpg" \
  -F "video=@video.mp4" \
  -F "challenge_id=EL_ID_DEL_RETO"
```

Si el `challenge_id` falta, venció, ya se usó o es de otro proyecto, responde `400` (`challenge_required`, `challenge_expired` o `challenge_invalid`) sin analizar el video.

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
    "liveness": { "anti_spoofing": {}, "face_tracking": {}, "lip_movement": {}, "lip_sync": {} },
    "challenge": { "head_turn": {}, "blink": {}, "hand_raise": {}, "gesture_order": {} }
  },
  "details": { "face": {}, "voice": {}, "liveness": {}, "challenge": {} },
  "errors": [],
  "meta": { "api_version": "1.0.0", "models": {} }
}
```

Cada check tiene la forma `{ "status": "passed | failed | not_evaluated", "code": "…", "message": "…" }`. El campo `code` solo viene cuando el check falla y es estable (por ejemplo `face_mismatch`, `spoof_detected` o `phrase_mismatch`), así que el cliente puede depender de él en lugar del mensaje. `not_evaluated` significa que falló un paso previo.

Los checks de `challenge` fallan con `head_turn_not_detected`, `blink_not_detected`, `hand_raise_not_detected` o `gesture_order_mismatch`. En `details.challenge` viene cuándo se detectó cada gesto y los valores máximos medidos (giro y parpadeo), útiles para calibrar. Sin reto (`REQUIRE_CHALLENGE=false`), el grupo trae solo `challenge_used` como `not_evaluated`.

### `POST /api/verify-photo`

Compara una foto de la persona contra la identificación. `multipart/form-data`:

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `id_image` | archivo de imagen | sí | Foto de la identificación |
| `photo` | archivo de imagen | sí | Foto de la persona (selfie) |
| `match_threshold_percent` | número 1–100 | no | % mínimo de similitud facial; default 70 |

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

La calidad solo se valida en la foto de la persona. La de la identificación se reporta en `details.id_face_quality`, pero no se valida, porque la foto de la INE suele ser chica. La respuesta tiene la misma forma general que la del video (`result`, `checks`, `details`, `meta`), y en `details.similarity` vienen `score`, `percent` y `threshold_score`.

Este modo es **más débil** que el de video: no hay sincronía de labios ni frase hablada, solo anti-spoofing sobre una imagen.

**Sobre los porcentajes de similitud facial:** el porcentaje es una curva sigmoide sobre la similitud coseno, pensada para mostrarse en pantalla. **No es una probabilidad.** La cara coincide si el porcentaje mostrado es mayor o igual a `match_threshold_percent` (70 por defecto, equivalente a una similitud coseno de ~0.41; 50 equivale a 0.363). En la respuesta vienen `threshold_percent` y `threshold_score` con el umbral que se usó.

---

## Configuración

Variables de entorno:

| Variable | Default | Descripción |
|---|---|---|
| `DATABASE_URL` | vacío | PostgreSQL de proyectos y sesiones. En Docker se arma solo con `POSTGRES_*` |
| `POSTGRES_PASSWORD` / `POSTGRES_USER` / `POSTGRES_DB` | — / `vida` / `vida` | Base de datos del `docker-compose.yml`; la contraseña es obligatoria |
| `PUBLIC_URL` | URL de la petición | URL pública para los enlaces de captura. **Defínela en producción** |
| `DEMO` | `false` | `true` solo en desarrollo: página de prueba en `/` y API directa sin llave si no hay `API_KEYS` |
| `SESSION_MINUTES` / `SESSION_MAX_ATTEMPTS` | `30` / `3` | Valores por defecto de cada sesión |
| `MAX_CONCURRENT_VERIFICATIONS` | `1` | Videos que se analizan a la vez; los demás esperan |
| `DELIVERY_TIMEOUT_SEC` / `DELIVERY_RETRIES` | `20` / `2` | Espera y reintentos del webhook |
| `ALLOW_HTTP_URLS` | `false` | Acepta `url_retorno` y `webhook_url` con `http://` (solo desarrollo) |
| `REQUIRE_CHALLENGE` | `true` | Exige `challenge_id` en `/api/verify-identity`. `false` solo para desarrollo o para migrar clientes viejos |
| `CHALLENGE_TTL_SEC` | `180` | Vigencia de un reto |
| `CHALLENGE_RATE_LIMIT_REQUESTS` | `30` | Retos por proyecto (o IP) en la ventana del rate limit |
| `EXPECTED_PHRASE` | `yo confirmo mi identidad y autorizo la verificacion de mi cuenta el dia de hoy` | Frase fija, solo sin reto (`REQUIRE_CHALLENGE=false`) |
| `ASR_MODEL_SIZE` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `ASR_DEVICE` | `cpu` | `cuda` si hay GPU |
| `ASR_COMPUTE_TYPE` | `int8` | `float16` con GPU |
| `ASR_CPU_THREADS` | `4` | Hilos de CPU para Whisper |
| `ASR_MAX_CONCURRENCY` | `1` | Transcripciones simultáneas (≈ núcleos / hilos) |
| `FACE_MODELS_DIR` | `models/` | Carpeta de los modelos descargados |
| `API_KEYS` | vacío | `proyecto:llave` separados por coma; vacío = sin autenticación (solo desarrollo) |
| `CORS_ORIGINS` | vacío | Orígenes de navegador permitidos, separados por coma; vacío = sin CORS |
| `FACE_MATCH_PERCENT` | `70` | % mínimo de similitud facial cuando el request no manda `match_threshold_percent` |
| `RATE_LIMIT_REQUESTS` | `10` | Verificaciones permitidas por proyecto (o por IP sin llaves) en la ventana; `0` lo desactiva |
| `RATE_LIMIT_WINDOW_SEC` | `60` | Duración de la ventana del rate limit |
| `MAX_VIDEO_MB` | `50` | Tamaño máximo del video |
| `MAX_IMAGE_MB` | `10` | Tamaño máximo de cada imagen |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | IPs del proxy de las que se acepta `X-Forwarded-For` |

Los umbrales (similitud facial, anti-spoofing, sincronía de labios, gestos, puntaje de la frase, duraciones) están en [`app/core/config.py`](app/core/config.py).

---

## Limitaciones conocidas

- **Las sesiones activas viven en memoria.** La INE (como vector) y el reto solo existen en el proceso: un reinicio deja las sesiones a medias como `expiradas` y su proyecto recibe `sesion.expirada`. Por lo mismo corre un solo contenedor de la API; para varias réplicas, ese estado temporal iría a Redis.
- **Los retos viven en memoria.** Un reinicio los borra (el usuario pide otro), y con más de un worker un reto pedido en uno no existe en otro. Para escalar a varios procesos habría que moverlos a Redis o similar.
- **Cámaras virtuales y deepfakes en vivo.** El reto frena videos pregrabados, pero no una cámara virtual que genere la cara en tiempo real siguiendo los gestos. Desde un navegador no hay una forma confiable de detectarlo.
- **Umbrales sin calibrar.** El de SFace (`0.363`) es el publicado por OpenCV, y los de sincronía de labios, gestos y calidad de foto son provisionales. Hay que ajustarlos con pares reales de INE y video o foto.
- No hay pruebas automatizadas.

---

## Licencias de los modelos

Todos permiten uso comercial:

- **YuNet**: MIT · **SFace**: Apache 2.0 ([OpenCV Zoo](https://github.com/opencv/opencv_zoo))
- **MiniFASNet**: Apache 2.0 ([Silent-Face-Anti-Spoofing](https://github.com/minivision-ai/Silent-Face-Anti-Spoofing)); ver [`app/assets/antispoof/NOTICE`](app/assets/antispoof/NOTICE)
- **MediaPipe Face Landmarker y Hand Landmarker**: Apache 2.0 (también la versión web `@mediapipe/tasks-vision` que usa la página de prueba)
- **Whisper**: MIT

Componentes de las sesiones: **PostgreSQL** (PostgreSQL License, permisiva), **asyncpg** (Apache 2.0) y **httpx** (BSD-3).

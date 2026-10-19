# Integración de la prueba de vida

Guía para el backend de cualquier proyecto que quiera agregar la prueba de vida a su flujo. No depende del lenguaje ni del framework: basta con poder hacer una petición HTTP `multipart/form-data`, redirigir al usuario y recibir un `POST` con JSON.

**URL del servicio:** `https://prueba-de-vida.buzzword.com.mx`

Solo pueden usarla los proyectos dados de alta: sin la `api_key` de un proyecto autorizado todo responde `401`.

---

## Si lo implementa con un asistente de IA

Pásele este documento completo y este mensaje, ajustando lo que está entre `< >`:

```text
Integra la prueba de vida descrita en INTEGRACION.md en este backend (<lenguaje y framework>).
Necesito:
1. Una función de servidor que cree la sesión (POST /api/v1/sesiones, multipart) con la foto de
   la INE que ya tenemos en <dónde está la INE>, una frase que cambie en cada trámite y
   url_retorno=<URL de nuestra pantalla siguiente>. Guarda el id de la sesión junto al trámite.
2. Redirigir al usuario a url_captura (o abrirla en <iframe | WebView>).
3. Un endpoint POST <ruta del webhook> que lea el cuerpo CRUDO, verifique X-Vida-Firma con
   HMAC-SHA256 (tolerancia 300 s, comparación en tiempo constante), ignore entregas repetidas
   por X-Vida-Entrega, guarde datos.resumen.aprobada y el resumen en el trámite, y responda 200
   solo después de guardar. Maneja sesion.completada, sesion.expirada y sesion.cancelada.
4. La api_key y el webhook_secreto se leen de variables de entorno PRUEBA_VIDA_API_KEY y
   PRUEBA_VIDA_WEBHOOK_SECRETO; nunca llegan al navegador ni a la app móvil.
5. Una prueba unitaria de la verificación de firma con el ejemplo de la sección "Verificar la firma".
6. (Si queremos video, fotos y cara de la INE) mandar incluir_video, incluir_fotogramas e
   incluir_rostro_ine en true al crear la sesión. Al recibir el webhook, descargar cada liga de
   datos.evidencia con X-API-Key antes de "expira" (en segundo plano, sin retrasar la respuesta 200),
   guardarlos cifrados y luego llamar DELETE /api/v1/sesiones/{id}/evidencia. Ver "Evidencia".
No uses los parámetros de url_retorno para decidir si se aprobó: la decisión viene del webhook.
```

---

## Cómo funciona

```
 Backend del proyecto                         Prueba de vida                        Usuario
 ────────────────────                         ──────────────                        ───────
 1. POST /api/v1/sesiones  ───────────────►  valida la INE
    (INE + frase + marca)                    ◄── { id, url_captura }
 2. redirige al usuario a url_captura  ─────────────────────────────────────────►  hace 3 gestos
                                                                                   y lee la frase
                                             3. analiza; si falla, el usuario
                                                reintenta (hasta max_intentos)
 4. recibe POST con JSON firmado  ◄───────  resultado: banderas y porcentajes
    lo guarda y responde 200  ──────────────►
 5.                                          regresa al usuario a url_retorno ─────►  sigue su trámite
```

En cada intento el usuario hace **tres gestos al azar** (girar la cabeza, parpadear y alzar una mano, en orden y lado que cambian cada vez) y después **lee en voz alta la frase que mandó el proyecto**. El servicio compara su cara con la INE, revisa que sea una persona real y que haya dicho la frase.

El servicio **no guarda** el video, la INE ni datos de la cara o la voz. Solo guarda un resumen de cada sesión (estado, decisión, intentos). Si el proyecto lo pide, el webhook trae ligas temporales para descargar el video, fotogramas y la cara de la INE del intento final; el servicio los borra al vencer las ligas o cuando el proyecto lo indica.

---

## Antes de empezar

Al dar de alta el proyecto, el equipo de la prueba de vida le entrega:

| Dato | Ejemplo | Para qué |
|---|---|---|
| `api_key` | `pv_9Xk…` | Autentica cada petición del backend. **Se muestra una sola vez.** |
| `webhook_secreto` | `whsec_Qa1…` | Comprueba que los resultados vienen del servicio |
| Webhook registrado | `https://api.su-proyecto.com/webhooks/vida` | A dónde llegan los resultados |
| Dominios permitidos | `su-proyecto.com` | Los únicos a los que se puede regresar al usuario o mandar resultados |

Las dos credenciales viven **solo en el backend**, nunca en el navegador ni en una app móvil.

El backend necesita:

- Hacer peticiones HTTPS con `multipart/form-data`.
- Exponer un endpoint HTTPS público que reciba un `POST` con JSON (el webhook).
- Calcular HMAC-SHA256, que existe en cualquier lenguaje.

---

## Paso 1. Crear la sesión

```
POST https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones
X-API-Key: <api_key>
Content-Type: multipart/form-data
```

### Campos

| Campo | Tipo | Obligatorio | Descripción |
|---|---|---|---|
| `ine_imagen` | archivo | **sí** | Foto de la INE, en binario. JPEG, PNG o WEBP; máximo 10 MB. El formato se revisa por el contenido, así que el `Content-Type` del archivo puede ser `application/octet-stream`. |
| `frase` | texto | **sí** | Lo que el usuario lee en voz alta. De 4 a 50 palabras. |
| `referencia_externa` | texto | no | Su identificador del cliente o trámite (máx. 200 caracteres). Vuelve en el resultado. |
| `url_retorno` | texto | no | URL `https://` a la que regresa el usuario al terminar. Debe ser de un dominio permitido. |
| `webhook_url` | texto | no | URL `https://` donde recibir el resultado, si es distinta a la registrada. |
| `max_intentos` | número | no | De 1 a 5. Por defecto 3. |
| `expira_minutos` | número | no | Vigencia del enlace, de 5 a 1440. Por defecto 30. |
| `umbral_rostro` | número | no | Porcentaje mínimo de parecido con la INE, de 1 a 100. Por defecto 70. |
| `metadata` | texto | no | Un objeto JSON en texto (máx. 4 KB), ej. `{"tramite":"alta"}`. Vuelve tal cual en el resultado. |
| `logo` | archivo | no | Logo para la pantalla del usuario. PNG, JPEG o WEBP; máximo 1 MB y 2000 px por lado. |
| `color_primario` | texto | no | Color hexadecimal (`#1B56D6` o `#18D`). Se usa en botones y en el paso actual. |
| `color_secundario` | texto | no | Color hexadecimal. Se usa en los pasos completados y en la animación de carga. |
| `incluir_video` | `true`/`false` | no | El webhook trae una liga al video del intento final, en MP4. Por defecto `false`. Ver [Evidencia](#evidencia-video-fotogramas-y-cara-de-la-ine). |
| `incluir_fotogramas` | `true`/`false` | no | El webhook trae ligas a 3 fotogramas JPEG con la cara de frente. Por defecto `false`. |
| `incluir_rostro_ine` | `true`/`false` | no | El webhook trae una liga al recorte de la cara de la INE, en JPEG. Por defecto `false`. |

Si no se mandan `logo` ni los colores, la pantalla usa el diseño genérico. El color del texto sobre el color primario (blanco o negro) se elige solo, para que siempre se lea.

**Sobre la frase:**

- Conviene que cambie en cada sesión, por ejemplo con el folio del trámite o el nombre del cliente. Así no se puede reusar una grabación.
- Los números van mejor en palabras o de máximo tres cifras: la transcripción escribe "4,521" y eso se compara mal.
- Solo se califica lo que el usuario dice mientras la frase está en pantalla: lo que diga durante los gestos, o un "listo" al final, no cuenta. Se aprueba con 85% de coincidencia o más.
- La lectura debe caber en el video junto con los gestos, por eso el máximo es 50 palabras (unos 25 segundos leyendo). Entre más larga, más fácil que el usuario se trabe: si se puede, que sea corta.

### Ejemplo

```bash
curl -X POST https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones \
  -H "X-API-Key: pv_..." \
  -F "ine_imagen=@ine.jpg" \
  -F "frase=acepto los términos y condiciones de mi crédito" \
  -F "referencia_externa=cliente-123" \
  -F "url_retorno=https://su-proyecto.com/registro/paso-4" \
  -F 'metadata={"tramite": "alta"}' \
  -F "logo=@logo.png" \
  -F "color_primario=#0F5C8C" \
  -F "color_secundario=#F5A623"
```

El mismo pedido en algunos lenguajes (el resto de los campos se agrega igual que `frase`):

<details>
<summary>Node.js 18+ (fetch nativo)</summary>

```js
const fs = require('fs');

const form = new FormData();
form.append('ine_imagen', new Blob([fs.readFileSync('ine.jpg')]), 'ine.jpg');
form.append('frase', frase);
form.append('referencia_externa', tramiteId);
form.append('url_retorno', 'https://su-proyecto.com/registro/paso-4');

const r = await fetch('https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones', {
  method: 'POST',
  headers: { 'X-API-Key': process.env.PRUEBA_VIDA_API_KEY },
  body: form,
});
if (r.status !== 201) throw new Error(`Prueba de vida: ${r.status} ${await r.text()}`);
const sesion = await r.json();   // guardar sesion.id; mandar al usuario a sesion.url_captura
```
</details>

<details>
<summary>Python (requests)</summary>

```python
import os, requests

with open("ine.jpg", "rb") as ine:
    r = requests.post(
        "https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones",
        headers={"X-API-Key": os.environ["PRUEBA_VIDA_API_KEY"]},
        files={"ine_imagen": ("ine.jpg", ine)},
        data={"frase": frase, "referencia_externa": tramite_id,
              "url_retorno": "https://su-proyecto.com/registro/paso-4"},
        timeout=60,
    )
r.raise_for_status()
sesion = r.json()   # guardar sesion["id"]; mandar al usuario a sesion["url_captura"]
```
</details>

<details>
<summary>PHP (cURL)</summary>

```php
$ch = curl_init('https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones');
curl_setopt_array($ch, [
    CURLOPT_POST => true,
    CURLOPT_RETURNTRANSFER => true,
    CURLOPT_TIMEOUT => 60,
    CURLOPT_HTTPHEADER => ['X-API-Key: ' . getenv('PRUEBA_VIDA_API_KEY')],
    CURLOPT_POSTFIELDS => [
        'ine_imagen' => new CURLFile('ine.jpg'),
        'frase' => $frase,
        'referencia_externa' => $tramiteId,
        'url_retorno' => 'https://su-proyecto.com/registro/paso-4',
    ],
]);
$cuerpo = curl_exec($ch);
if (curl_getinfo($ch, CURLINFO_HTTP_CODE) !== 201) throw new Exception("Prueba de vida: $cuerpo");
$sesion = json_decode($cuerpo, true);   // guardar $sesion['id']; redirigir a $sesion['url_captura']
```
</details>

### Respuesta `201`

```json
{
  "id": "ses_81ef8df968b44104a8ccfc499e993b14",
  "referencia_externa": "cliente-123",
  "frase": "acepto los términos y condiciones de mi crédito",
  "estado": "pendiente",
  "aprobada": null,
  "intentos": 0,
  "max_intentos": 3,
  "checks_fallidos": [],
  "url_captura": "https://prueba-de-vida.buzzword.com.mx/captura/IgsF1aUh...",
  "creada": "2026-10-14T18:05:01+00:00",
  "expira": "2026-10-14T18:35:01+00:00",
  "finalizada": null,
  "entrega": null,
  "metadata": { "tramite": "alta" }
}
```

Guarden el `id` junto con su trámite: con él se relaciona el resultado.

### Errores

Todos responden JSON con un campo `detail` que explica el problema.

| Código | Causa |
|---|---|
| `401` | Falta la API key o no es válida |
| `400` | `ine_imagen` no es una imagen válida |
| `413` | `ine_imagen` pesa más de 10 MB, o `logo` más de 1 MB |
| `422` | Falta un campo obligatorio; la INE no tiene un rostro visible; la frase tiene menos de 4 o más de 50 palabras; un color no es hexadecimal; el logo no es PNG, JPEG o WEBP; `url_retorno` o `webhook_url` no son `https://` o no son de un dominio permitido; `metadata` no es un objeto JSON |
| `429` | Demasiadas sesiones por minuto. Esperar los segundos del header `Retry-After` |
| `503` | El servicio no está disponible |

---

## Paso 2. Mandar al usuario a la prueba

Cualquiera de estas formas funciona, en computadora o celular:

**Redirección (la más simple).** Mandar el navegador del usuario a `url_captura`. Al terminar, el servicio lo regresa a:

```
<url_retorno>?sesion_id=ses_...&estado=completada&aprobada=1
```

`aprobada` vale `1` (aprobado) o `0` (no aprobado). **Este parámetro sirve solo para mostrarle algo al usuario**: él podría modificar la URL. La decisión real es la que llega por el webhook.

**Iframe.** Incrustar `url_captura` con permiso de cámara y micrófono. El dominio de la página debe estar entre los permitidos:

```html
<iframe src="URL_CAPTURA" allow="camera; microphone" style="width:100%;height:100vh;border:0"></iframe>
```

Al terminar, el iframe no redirige: le avisa a la página con `postMessage`:

```
origen:  https://prueba-de-vida.buzzword.com.mx
mensaje: { "tipo": "prueba-vida:finalizada", "sesion_id": "ses_...", "estado": "completada", "aprobada": true }
```

**App móvil.** Abrir `url_captura` en un WebView con permisos de cámara y micrófono, y detectar la navegación a `url_retorno` para cerrarlo.

La cámara del navegador solo funciona con HTTPS, y `url_captura` siempre lo es.

---

## Paso 3. Recibir el resultado (webhook)

Cuando la sesión termina, el servicio manda:

```
POST <webhook_url>
Content-Type: application/json
X-Vida-Evento:  sesion.completada
X-Vida-Entrega: evt_8e516baa65dc4ed2a26739d05a4c1eb2
X-Vida-Firma:   t=1760000000,v1=8022fa202f251266714d9592170e7e0108d3607f928e791830fe6f9c3ea188f8
```

### Eventos

| `X-Vida-Evento` | Cuándo | Qué trae |
|---|---|---|
| `sesion.completada` | El usuario aprobó, o se agotaron los intentos | `sesion`, `resumen`, `intentos` y `resultado` |
| `sesion.expirada` | El enlace venció sin terminar, o el servicio se reinició a media sesión | Solo `sesion` |
| `sesion.cancelada` | El proyecto canceló la sesión | Solo `sesion` |

### Cuerpo

```json
{
  "id": "evt_8e516baa65dc4ed2a26739d05a4c1eb2",
  "evento": "sesion.completada",
  "creado": "2026-10-14T18:08:26+00:00",
  "datos": {
    "sesion": {
      "id": "ses_81ef8df968b44104a8ccfc499e993b14",
      "referencia_externa": "cliente-123",
      "frase": "acepto los términos y condiciones de mi crédito",
      "estado": "completada",
      "aprobada": true,
      "intentos": 2,
      "max_intentos": 3,
      "checks_fallidos": [],
      "creada": "2026-10-14T18:05:01+00:00",
      "finalizada": "2026-10-14T18:08:26+00:00",
      "metadata": { "tramite": "alta" }
    },
    "resumen": {
      "aprobada": true,
      "checks_fallidos": [],
      "motivos": [],
      "rostro": {
        "coincide": true,
        "similitud_porcentaje": 88.4,
        "similitud_minima_porcentaje": 86.2,
        "umbral_porcentaje": 70.0,
        "una_sola_persona": true,
        "misma_persona": true
      },
      "frase": {
        "coincide": true,
        "coincidencia_porcentaje": 96.5,
        "umbral_porcentaje": 85.0,
        "esperada": "acepto los términos y condiciones de mi crédito",
        "transcripcion": "Acepto los términos y condiciones de mi crédito."
      },
      "vida": {
        "persona_real": true,
        "probabilidad_real_porcentaje": 93.1,
        "umbral_porcentaje": 50.0,
        "labios_sincronizados": true
      },
      "gestos": {
        "completos": true,
        "en_orden": true,
        "detalle": [
          { "gesto": "turn_left", "detectado": true, "segundo": 1.4 },
          { "gesto": "blink", "detectado": true, "segundo": 3.1 },
          { "gesto": "raise_right_hand", "detectado": true, "segundo": 5.0 }
        ]
      }
    },
    "intentos": [
      { "numero": 1, "aprobado": false, "checks_fallidos": ["challenge.blink"], "duracion_ms": 5120, "creado": "…" },
      { "numero": 2, "aprobado": true, "checks_fallidos": [], "duracion_ms": 4870, "creado": "…" }
    ],
    "resultado": { "…": "reporte técnico completo del último intento" },
    "evidencia": null
  }
}
```

`evidencia` es `null` salvo que la sesión la haya pedido; ver [Evidencia](#evidencia-video-fotogramas-y-cara-de-la-ine).

### Qué guardar

**La decisión final es `datos.resumen.aprobada`** (igual a `datos.sesion.aprobada`). Solo es `true` si pasaron todas las validaciones.

`datos.resumen` trae, por cada validación, una bandera y sus porcentajes. Cada bandera vale:

- `true`: pasó.
- `false`: falló.
- `null`: no se pudo evaluar porque falló un paso previo. Por ejemplo, si no se vio la cara en el video no hay similitud.

| Campo | Qué dice |
|---|---|
| `rostro.coincide` | La cara del video es la de la INE. Se compara mientras el usuario lee la frase, que es cuando está quieto y de frente; `similitud_porcentaje` es ese parecido y debe llegar a `umbral_porcentaje`. **Usar `similitud_minima_porcentaje` para mostrar el dato que decide** |
| `rostro.misma_persona` | Quien hizo los gestos es la misma persona que leyó la frase. Evita que alguien haga los gestos y otra persona (o un video) lea la frase. `null` si el video no trae el tramo de la frase; entonces la INE se compara en los 3 tramos del video y todos deben llegar al umbral |
| `rostro.una_sola_persona` | Solo había una persona frente a la cámara |
| `frase.coincide` | Dijo la frase. `transcripcion` es lo que se escuchó |
| `vida.persona_real` | Es una persona real, no una foto, una pantalla ni una máscara |
| `vida.labios_sincronizados` | La boca se movió al ritmo de la voz |
| `gestos.completos` / `gestos.en_orden` | Hizo los tres gestos, y en el orden pedido |
| `motivos` | Por qué no pasó, en palabras para el usuario. Vacío si se aprobó |
| `checks_fallidos` | Los mismos motivos como códigos estables, ej. `face.face_match`, `face.same_person`, `voice.phrase_match`, `challenge.blink` |

`datos.resultado` es el reporte técnico completo, por si se quiere el detalle.

Los códigos de gesto posibles son `turn_left`, `turn_right`, `blink`, `raise_left_hand` y `raise_right_hand`. Izquierda y derecha son las del usuario.

### Evidencia: video, fotogramas y cara de la INE

Solo si la sesión se creó con `incluir_video`, `incluir_fotogramas` o `incluir_rostro_ine` en `true`. El webhook **no trae los archivos**: trae ligas temporales para descargarlos.

```json
"evidencia": {
  "intento": 2,
  "expira": "2026-10-15T19:00:00+00:00",
  "video": {
    "url": "https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones/ses_81ef.../evidencia/video",
    "tipo": "video/mp4", "bytes": 690226
  },
  "fotogramas": [
    { "orden": 1, "segundo": 1.4, "de_frente": true,
      "url": "https://.../api/v1/sesiones/ses_81ef.../evidencia/fotograma/1", "tipo": "image/jpeg", "bytes": 56276 },
    { "orden": 2, "segundo": 3.1, "de_frente": true, "url": ".../evidencia/fotograma/2", "tipo": "image/jpeg", "bytes": 57031 },
    { "orden": 3, "segundo": 5.0, "de_frente": true, "url": ".../evidencia/fotograma/3", "tipo": "image/jpeg", "bytes": 57182 }
  ],
  "rostro_ine": {
    "url": "https://.../api/v1/sesiones/ses_81ef.../evidencia/rostro-ine", "tipo": "image/jpeg", "bytes": 12480
  }
}
```

| Campo | Qué es |
|---|---|
| `intento` | El intento al que pertenecen: el que decidió la sesión (el aprobado, o el último si se agotaron). De los anteriores no se entrega nada. |
| `expira` | Hasta cuándo se pueden descargar: 60 minutos después de terminar la sesión (nunca menos de 30). |
| `video` | El video completo, en **MP4** (H.264 + AAC), venga del navegador que venga. Si la conversión fallara, llega el original y `tipo` lo dice (`video/webm`). `null` si no se pidió. |
| `fotogramas` | 3 cuadros completos (640×480) en JPEG, uno por cada tercio del video, con **una sola cara y de frente**: en cada tercio se elige el cuadro más derecho entre los nítidos. `de_frente` es `false` si en ese tercio la persona nunca miró de frente (estaba haciendo un gesto); entonces va el menos girado. Vacío si no se pidieron. |
| `rostro_ine` | Recorte de la cara de la INE con un margen alrededor, en JPEG. `null` si no se pidió. |

Si se pidió evidencia pero no se pudo generar, `expira` es `null` y los campos vienen vacíos.

**Descargar.** Con la misma `api_key` del proyecto:

```bash
curl -H "X-API-Key: pv_..." -o video.mp4 "https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones/ses_.../evidencia/video"
```

Se puede descargar cada archivo las veces que haga falta hasta `expira`. Al terminar, borrarla:

```bash
curl -X DELETE -H "X-API-Key: pv_..." "https://prueba-de-vida.buzzword.com.mx/api/v1/sesiones/ses_.../evidencia"
```

| Respuesta | Significa |
|---|---|
| `200` | El archivo, con su `Content-Type` |
| `204` | (`DELETE`) Evidencia borrada |
| `401` | Falta la `api_key` o no es válida |
| `404` | La sesión no existe, es de otro proyecto, o ese archivo no forma parte de la evidencia |
| `410` | La evidencia ya venció o se borró |

**Recomendaciones:**

- Responder el webhook con `200` primero y descargar en segundo plano: el webhook tiene 20 segundos.
- Hacer el `DELETE` solo después de guardar todo. No se borra al descargar, para que un corte de red no la pierda.
- Si el servicio se reinicia antes de que la descarguen, la evidencia se pierde (`410`).
- **Responsabilidad:** el video y las fotos son datos personales sensibles. Al descargarlos quedan bajo resguardo del proyecto: guardarlos cifrados, con acceso restringido y por un tiempo definido, y mencionarlo en el aviso de privacidad.

### Verificar la firma

Antes de usar el resultado hay que comprobar que viene del servicio:

1. Leer el **cuerpo crudo** de la petición, tal como llegó en bytes, sin parsearlo ni reformatearlo antes.
2. Separar el header `X-Vida-Firma` por comas y luego por `=`. Se obtienen `t` (segundos Unix) y `v1` (firma en hexadecimal).
3. Rechazar si `t` difiere más de **300 segundos** de la hora actual.
4. Calcular `HMAC-SHA256` con la llave `webhook_secreto`, sobre el mensaje `t` + `"."` + cuerpo crudo.
5. Pasarlo a hexadecimal en minúsculas y compararlo con `v1` **en tiempo constante** (la función de comparación segura de su lenguaje).
6. Si no coincide, responder `401` y descartar.

Pseudocódigo:

```
cuerpo   = bytes_crudos(peticion)
partes   = separar(header["X-Vida-Firma"], ",", "=")       # { t: "...", v1: "..." }
si |ahora_unix() - entero(partes.t)| > 300:  responder 401
esperado = hex(hmac_sha256(llave = webhook_secreto, mensaje = partes.t + "." + cuerpo))
si no comparar_tiempo_constante(esperado, partes.v1):  responder 401
evento   = parsear_json(cuerpo)
```

**Ejemplo para probar su implementación.** Con estos valores, su código debe dar exactamente esta firma:

| | Valor |
|---|---|
| `webhook_secreto` | `whsec_prueba123` |
| `t` | `1760000000` |
| cuerpo | `{"id":"evt_123","evento":"sesion.completada"}` |
| `v1` esperado | `8022fa202f251266714d9592170e7e0108d3607f928e791830fe6f9c3ea188f8` |

Desde una terminal:

```bash
printf '%s' '1760000000.{"id":"evt_123","evento":"sesion.completada"}' | openssl dgst -sha256 -hmac 'whsec_prueba123'
```

(Ese ejemplo es solo para comparar la firma: su `t` real tendrá más de 300 segundos de antigüedad y el paso 3 lo rechazaría.)

**El error más común** es calcular la firma sobre el JSON ya parseado y vuelto a serializar: cambian espacios o el orden y la firma no coincide. Hay que usar los bytes tal como llegaron (en Express, `express.raw`; en Flask, `request.get_data()`; en FastAPI, `await request.body()`; en Django, `request.body`; en Laravel, `$request->getContent()`; en ASP.NET, leer `Request.Body` antes de cualquier binding; en Spring, recibir `@RequestBody byte[]`).

Implementaciones de referencia (todas dan `true` con el ejemplo de arriba si se quita la revisión de los 300 segundos):

<details>
<summary>Node.js (Express)</summary>

```js
const crypto = require('crypto');

function firmaValida(secreto, cabecera, cuerpo /* Buffer */) {
  const partes = Object.fromEntries(String(cabecera || '').split(',').map((p) => p.split('=', 2)));
  const t = Number(partes.t);
  if (!Number.isInteger(t) || !partes.v1) return false;
  if (Math.abs(Date.now() / 1000 - t) > 300) return false;
  const esperado = crypto.createHmac('sha256', secreto)
    .update(Buffer.concat([Buffer.from(`${partes.t}.`), cuerpo])).digest('hex');
  return esperado.length === partes.v1.length
    && crypto.timingSafeEqual(Buffer.from(esperado), Buffer.from(partes.v1));
}

app.post('/webhooks/vida', express.raw({ type: 'application/json' }), async (req, res) => {
  if (!firmaValida(process.env.PRUEBA_VIDA_WEBHOOK_SECRETO, req.get('X-Vida-Firma'), req.body)) {
    return res.sendStatus(401);
  }
  const evento = JSON.parse(req.body);
  // if (yaProcesado(evento.id)) return res.sendStatus(200);
  // guardar evento.datos.sesion.id, evento.evento, evento.datos.resumen ...
  res.sendStatus(200);
});
```
</details>

<details>
<summary>Python</summary>

```python
import hashlib, hmac, time

def firma_valida(secreto: str, cabecera: str, cuerpo: bytes) -> bool:
    try:
        partes = dict(p.split("=", 1) for p in cabecera.split(","))
        t = int(partes["t"])
    except (ValueError, KeyError):
        return False
    if abs(time.time() - t) > 300:
        return False
    esperado = hmac.new(secreto.encode(), f"{partes['t']}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, partes.get("v1", ""))
```
</details>

<details>
<summary>PHP</summary>

```php
function firmaValida(string $secreto, string $cabecera, string $cuerpo): bool {
    $partes = [];
    foreach (explode(',', $cabecera) as $p) {
        [$k, $v] = array_pad(explode('=', $p, 2), 2, '');
        $partes[$k] = $v;
    }
    if (!isset($partes['t'], $partes['v1']) || !ctype_digit($partes['t'])) return false;
    if (abs(time() - (int) $partes['t']) > 300) return false;
    $esperado = hash_hmac('sha256', $partes['t'] . '.' . $cuerpo, $secreto);
    return hash_equals($esperado, $partes['v1']);
}

// $cuerpo = file_get_contents('php://input');
// $cabecera = $_SERVER['HTTP_X_VIDA_FIRMA'] ?? '';
```
</details>

<details>
<summary>C# (.NET 6+)</summary>

```csharp
using System.Security.Cryptography;
using System.Text;

static bool FirmaValida(string secreto, string cabecera, byte[] cuerpo)
{
    var partes = new Dictionary<string, string>();
    foreach (var p in cabecera.Split(','))
    {
        var kv = p.Split('=', 2);
        if (kv.Length == 2) partes[kv[0]] = kv[1];
    }
    if (!partes.TryGetValue("t", out var t) || !partes.TryGetValue("v1", out var v1)
        || !long.TryParse(t, out var ts)) return false;
    if (Math.Abs(DateTimeOffset.UtcNow.ToUnixTimeSeconds() - ts) > 300) return false;
    var mensaje = Encoding.UTF8.GetBytes(t + ".").Concat(cuerpo).ToArray();
    var esperado = Convert.ToHexString(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secreto), mensaje)).ToLowerInvariant();
    return CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(esperado), Encoding.ASCII.GetBytes(v1));
}
```
</details>

<details>
<summary>Java 17+</summary>

```java
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.HashMap;
import java.util.HexFormat;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

static boolean firmaValida(String secreto, String cabecera, byte[] cuerpo) throws Exception {
    Map<String, String> partes = new HashMap<>();
    for (String p : cabecera.split(",")) {
        String[] kv = p.split("=", 2);
        if (kv.length == 2) partes.put(kv[0], kv[1]);
    }
    String t = partes.get("t"), v1 = partes.get("v1");
    if (t == null || v1 == null) return false;
    long ts;
    try { ts = Long.parseLong(t); } catch (NumberFormatException e) { return false; }
    if (Math.abs(Instant.now().getEpochSecond() - ts) > 300) return false;
    Mac mac = Mac.getInstance("HmacSHA256");
    mac.init(new SecretKeySpec(secreto.getBytes(StandardCharsets.UTF_8), "HmacSHA256"));
    mac.update((t + ".").getBytes(StandardCharsets.UTF_8));
    String esperado = HexFormat.of().formatHex(mac.doFinal(cuerpo));
    return MessageDigest.isEqual(esperado.getBytes(StandardCharsets.UTF_8), v1.getBytes(StandardCharsets.UTF_8));
}
```
</details>

<details>
<summary>Go</summary>

```go
import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"strconv"
	"strings"
	"time"
)

func firmaValida(secreto, cabecera string, cuerpo []byte) bool {
	partes := map[string]string{}
	for _, p := range strings.Split(cabecera, ",") {
		if k, v, ok := strings.Cut(p, "="); ok {
			partes[k] = v
		}
	}
	ts, err := strconv.ParseInt(partes["t"], 10, 64)
	if err != nil {
		return false
	}
	if d := time.Now().Unix() - ts; d > 300 || d < -300 {
		return false
	}
	mac := hmac.New(sha256.New, []byte(secreto))
	mac.Write([]byte(partes["t"] + "."))
	mac.Write(cuerpo)
	esperado := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(esperado), []byte(partes["v1"]))
}
```
</details>

### Cómo responder

- **Responder `2xx` solo después de guardar el resultado.** Para el servicio, esa respuesta significa "ya lo tengo".
- Hay **20 segundos** para responder. Si no responde o responde otro código, el servicio reintenta 2 veces más. Si sigue fallando, el usuario ve "No se pudo registrar el resultado" con un botón para reintentar el envío, mientras la sesión no venza.
- Por los reintentos, un mismo evento puede llegar más de una vez. Usar `X-Vida-Entrega` (igual a `id` del cuerpo) para ignorar duplicados.
- El cuerpo de su respuesta no se usa.

---

## Consultas opcionales

Con el mismo header `X-API-Key`. Cada proyecto solo ve sus sesiones.

| Método | Ruta | Para qué |
|---|---|---|
| `GET` | `/api/v1/sesiones/{id}` | Estado de una sesión y de sus intentos |
| `GET` | `/api/v1/sesiones?estado=&limite=` | Sesiones del proyecto, de la más reciente a la más vieja (`limite` hasta 500) |
| `POST` | `/api/v1/sesiones/{id}/cancelar` | Cancelar una sesión activa. Llega el webhook `sesion.cancelada` |
| `GET` | `/api/v1/sesiones/{id}/evidencia/video`, `/fotograma/{1-3}`, `/rostro-ine` | Descargar la evidencia (ver [Evidencia](#evidencia-video-fotogramas-y-cara-de-la-ine)) |
| `DELETE` | `/api/v1/sesiones/{id}/evidencia` | Borrar la evidencia ya descargada |

Las consultas no traen el reporte ni la frase de sesiones viejas: el resultado completo solo llega por el webhook.

### Estados de una sesión

| Estado | Significa |
|---|---|
| `pendiente` | Esperando al usuario, o a que reintente |
| `procesando` | Analizando el video |
| `entregando` | Enviando el resultado al webhook |
| `completada` | Terminó y el proyecto confirmó la recepción. Ver `aprobada` |
| `error_entrega` | Terminó pero el webhook no confirmó. Se puede reintentar |
| `expirada` | Venció sin terminar |
| `cancelada` | La canceló el proyecto |

---

## Lista de verificación

- [ ] La `api_key` y el `webhook_secreto` están en el backend, en variables de entorno o un gestor de secretos.
- [ ] La sesión se crea desde el backend, nunca desde el navegador.
- [ ] La frase cambia en cada sesión y tiene de 4 a 50 palabras.
- [ ] El webhook es HTTPS, público y verifica la firma con el cuerpo crudo.
- [ ] El webhook responde `2xx` solo después de guardar, e ignora entregas repetidas.
- [ ] La decisión se toma con `datos.resumen.aprobada` del webhook, no con los parámetros de `url_retorno`.
- [ ] Se manejan los eventos `sesion.expirada` y `sesion.cancelada`, por ejemplo para ofrecer un enlace nuevo.
- [ ] El usuario da su consentimiento para el uso de su imagen y voz antes de iniciar (son datos personales sensibles).
- [ ] Si se pide evidencia: se descarga antes de `expira`, sin retrasar la respuesta del webhook; se guarda cifrada y por un tiempo definido; y se llama al `DELETE` al terminar.

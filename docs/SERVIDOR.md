# Prueba de vida: guía del servidor

Para quien administra el servidor donde corre `https://prueba-de-vida.buzzword.com.mx`.

**Qué cambia con esta versión:** hoy el servicio está abierto (cualquiera puede pedir un reto y usar la página de prueba). Con esta versión **solo entran los proyectos dados de alta**: cada uno tiene su propia API key, los dominios a los que puede regresar al usuario y la URL donde recibe los resultados. Se agrega un contenedor de PostgreSQL para guardar los proyectos y un resumen de cada sesión (nunca el video, la INE ni datos de la cara o la voz).

---

## 1. Requisitos

- Linux **amd64** con Docker y el plugin `docker compose` (`docker compose version`).
- 4 núcleos y 8 GB de RAM como mínimo; cada análisis de video ocupa varios núcleos unos segundos.
- Un proxy HTTPS (nginx, Traefik, balanceador, Cloudflare…) que publique el dominio con certificado. **Sin HTTPS la cámara del navegador no funciona.**
- Salida a internet desde el contenedor hacia los webhooks de los proyectos (HTTPS, puerto 443).

## 2. Actualizar el código

```bash
cd /ruta/del/proyecto          # donde ya está clonado
cp .env .env.respaldo-$(date +%F)
git fetch origin
git checkout prueba-de-vida-produccion      # la rama de producción
git pull
```

## 3. Configurar `.env`

Comparar el `.env` actual con [`.env.example`](../.env.example) y agregar lo que falte. Lo obligatorio:

```bash
# Generar la contraseña de la base (una sola vez; no cambiarla después):
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

```ini
PUBLIC_URL=https://prueba-de-vida.buzzword.com.mx
POSTGRES_PASSWORD=<la generada arriba>
DEMO=false
API_KEYS=
```

`DEMO=false` es lo que cierra el servicio: quita la página de prueba de `/` y hace que todo pida la llave de un proyecto. `API_KEYS` se deja vacío: los proyectos se dan de alta en la base (paso 6).

Según dónde esté el proxy HTTPS:

| Proxy | `API_BIND` | `FORWARDED_ALLOW_IPS` |
|---|---|---|
| En **este mismo** servidor (recomendado) | `127.0.0.1` | `*` |
| En **otra** máquina o balanceador | `0.0.0.0` | IP del proxy, ej. `10.0.0.5` |

Con `API_BIND=127.0.0.1` el puerto 7860 solo se alcanza desde el propio servidor, así nadie entra por `http://IP:7860` saltándose el certificado. `FORWARDED_ALLOW_IPS` sirve para que los límites de uso cuenten por la IP real del cliente y no por la del proxy (con el proxy local, Docker ve la IP de su red interna, por eso `*`, que es seguro solo si el puerto está cerrado hacia afuera).

## 4. Levantar

```bash
docker compose up -d --build
docker compose ps                 # api y db deben decir "healthy" (la api tarda ~1 min)
docker compose logs -f api        # Ctrl+C para salir
```

La primera construcción tarda varios minutos (descarga modelos). Para que todo arranque solo al prender el servidor (una vez):

```bash
sudo systemctl enable docker
```

## 5. Proxy HTTPS

El proxy debe apuntar a `http://127.0.0.1:7860` (o `http://<servidor>:7860` si está en otra máquina) y:

- Aceptar cuerpos de **64 MB** (los videos pesan hasta 50 MB).
- Esperar respuestas de hasta **3 minutos**.
- Pasar `X-Forwarded-For` y `X-Forwarded-Proto`.
- **No** mandar un `Permissions-Policy` que bloquee `camera` o `microphone`, ni un `X-Frame-Options`/`Content-Security-Policy` propio en `/captura/` (el servicio ya pone el suyo para que solo los dominios de cada proyecto puedan incrustarla).

Ejemplo con nginx:

```nginx
server {
    listen 443 ssl http2;
    server_name prueba-de-vida.buzzword.com.mx;
    # ssl_certificate / ssl_certificate_key como ya los tengan

    client_max_body_size 64m;

    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 180s;
        proxy_send_timeout 180s;
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Si el puerto 7860 estaba abierto en el firewall y el proxy es local, cerrarlo (ej. `sudo ufw delete allow 7860`).

## 6. Dar de alta los proyectos

Por cada proyecto se necesitan tres datos (los pasa el responsable del proyecto):

| Dato | Ejemplo |
|---|---|
| Nombre | `Créditos` |
| URL del webhook (HTTPS, pública) donde recibe resultados | `https://api.creditos.com/webhooks/vida` |
| Dominios permitidos (a donde se regresa al usuario, donde se incrusta la captura y a donde van los webhooks) | `creditos.com` (incluye sus subdominios) |

```bash
docker compose exec api python -m app.cli crear "Créditos" \
    --webhook https://api.creditos.com/webhooks/vida \
    --dominios creditos.com
```

Responde un JSON con `id`, `api_key` y `webhook_secreto`. **La `api_key` se muestra una sola vez** (en la base solo queda su hash). Mandar `api_key` y `webhook_secreto` al responsable del proyecto por un canal seguro (gestor de contraseñas, mensaje que se autodestruye; no por correo ni en un chat grupal), junto con [`INTEGRACION.md`](INTEGRACION.md).

Siempre usar `--dominios` en producción: sin él, el proyecto podría regresar al usuario o mandar resultados a cualquier dominio.

Otros comandos:

```bash
docker compose exec api python -m app.cli listar
docker compose exec api python -m app.cli rotar-key prj_...       # llave nueva; la anterior deja de servir al instante
docker compose exec api python -m app.cli desactivar prj_...      # corta el acceso del proyecto
docker compose exec api python -m app.cli activar prj_...
docker compose exec api python -m app.cli webhook prj_... https://nueva-url/webhooks/vida
docker compose exec api python -m app.cli dominios prj_... creditos.com,otrodominio.com
```

Si un proyecto ya consumía la API directa (`/api/challenge`, `/api/verify-identity`, `/api/verify-photo`) sin llave, después de este cambio recibirá `401`: debe mandar su `api_key` en el header `X-API-Key`.

### Panel administrativo

En `https://<dominio>/panel` (y `/` lleva ahí) hay un tablero con las sesiones e intentos de todos los proyectos: totales, gráficas, una tabla con filtros y el detalle de cada sesión. Solo lee el resumen que ya está en la base; no muestra video, INE ni datos de la cara.

Tiene una sola cuenta. Para activarla, en `.env`:

```bash
# Hash de la contraseña (la pide dos veces; no se guarda en claro):
docker compose exec api python -m app.core.acceso hash
```

```ini
ADMIN_EMAIL=correo@empresa.com
ADMIN_PASSWORD_HASH=scrypt$...      # lo que imprimió el comando anterior
SECRET_KEY=...                      # python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Luego `docker compose up -d`. Sin `SECRET_KEY` el panel funciona, pero hay que volver a entrar tras cada reinicio. El login se bloquea unos minutos tras 5 intentos fallidos desde la misma IP (30 en total).

## 7. Comprobar que todo está bien

Desde cualquier equipo con `curl` (no hace falta estar en el servidor):

```bash
scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx
```

Revisa que el servicio responda, que la página de prueba no exista y que sin llave, o con una llave falsa, todo se rechace. Debe terminar en `Todo bien.`

Con la llave de un proyecto revisa además que ese proyecto sí entre; con una foto de INE, crea una sesión de prueba, muestra su `url_captura` y la cancela (el webhook del proyecto recibe `sesion.cancelada`):

```bash
scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx pv_LLAVE_DEL_PROYECTO
scripts/verificar-produccion.sh https://prueba-de-vida.buzzword.com.mx pv_LLAVE_DEL_PROYECTO ine.jpg
```

Revisión manual del puerto, desde **fuera** del servidor (con `API_BIND=127.0.0.1` debe fallar la conexión):

```bash
curl -m 5 http://IP_DEL_SERVIDOR:7860/health
```

## 8. Operación diaria

**Actualizar** (después de cada `git pull`):

```bash
docker compose up -d --build
```

Las sesiones a medias se pierden con el reinicio (la INE solo vive en memoria): quedan como `expiradas` y se le avisa a su proyecto por webhook. Conviene actualizar en horario de poco uso.

**Logs:** `docker compose logs --since 1h api`. Ya rotan solos (5 archivos de 20 MB por contenedor). Las entregas fallidas a un webhook aparecen como `Entrega de ... fallo`.

**Respaldo diario de la base** (solo proyectos y resúmenes; es chica). Con `crontab -e`:

```cron
0 3 * * * cd /ruta/del/proyecto && docker compose exec -T db pg_dump -U vida vida | gzip > /respaldos/vida-$(date +\%F).sql.gz
```

Restaurar:

```bash
gunzip -c /respaldos/vida-AAAA-MM-DD.sql.gz | docker compose exec -T db psql -U vida vida
```

**No borrar el volumen `pgdata`** (`docker compose down -v`): ahí están los proyectos y sus llaves. `docker compose down` sin `-v` es seguro.

**Más capacidad:** `MAX_CONCURRENT_VERIFICATIONS` (videos analizados a la vez; aprox. núcleos ÷ 4) y `RATE_LIMIT_REQUESTS` en `.env`, luego `docker compose up -d`. La API debe seguir con un solo worker: los límites se cuentan en memoria.

## 9. Problemas comunes

| Síntoma | Causa probable |
|---|---|
| `docker compose up` dice `Falta POSTGRES_PASSWORD` | No está en `.env` |
| La api no llega a `healthy` | Ver `docker compose logs api`; suele ser falta de RAM o la base sin arrancar |
| `/` sigue mostrando la página de prueba | `DEMO` no es `false`, o no se reconstruyó (`up -d --build`) |
| Sesiones responden `503` | La api no se conecta a la base: revisar `docker compose ps db` |
| `413` al subir el video | El proxy limita el cuerpo: `client_max_body_size 64m` |
| `504` al terminar la grabación | Timeout del proxy menor a 180 s |
| La cámara no abre | La página no está en `https://`, o el proxy manda `Permissions-Policy` que la bloquea |
| La captura no carga dentro de un iframe | El dominio de la página no está en `--dominios` del proyecto |
| El usuario ve "No se pudo registrar el resultado" | El webhook del proyecto no respondió `2xx` en 20 s; ver logs y avisar al proyecto |
| `422 ... no pertenece a los dominios permitidos` | `url_retorno` o `webhook_url` fuera de los dominios del proyecto |

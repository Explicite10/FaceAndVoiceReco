"""Proyecto cliente de ejemplo: integra la prueba de vida como lo haria cualquier sistema.

1. El usuario sube su INE en este sitio.
2. Este backend crea la sesion (con la API key) y lo manda a url_captura.
3. La prueba de vida entrega el resultado firmado en /webhooks/vida.
4. El usuario regresa a /regreso y ve el resultado que llego por el webhook.

Probar en local (la API en :7860 con ALLOW_HTTP_URLS=true):
    python -m app.cli crear "Demo" --webhook http://127.0.0.1:8100/webhooks/vida
    VIDA_API_KEY=pv_... VIDA_WEBHOOK_SECRETO=whsec_... \\
        uvicorn ejemplos.proyecto_demo:app --port 8100
    abrir http://127.0.0.1:8100
"""
import hashlib
import hmac
import html
import json
import os
import time

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

VIDA_URL = os.getenv("VIDA_URL", "http://127.0.0.1:7860")
API_KEY = os.environ["VIDA_API_KEY"]
SECRETO = os.environ["VIDA_WEBHOOK_SECRETO"]
AQUI = os.getenv("DEMO_URL", "http://127.0.0.1:8100")

app = FastAPI(title="Proyecto demo")
# En un sistema real esto va a la base de datos del proyecto.
resultados: dict[str, dict] = {}

_PAGINA = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Proyecto demo</title><body style="font-family:system-ui;max-width:640px;margin:40px auto;padding:0 16px">{}</body>"""


@app.get("/", response_class=HTMLResponse)
def inicio():
    return _PAGINA.format("""
      <h2>Alta de cliente (demo)</h2>
      <form method="post" action="/iniciar" enctype="multipart/form-data">
        <p><label>Referencia del cliente <input name="referencia" value="cliente-123"></label></p>
        <p><label>Frase a leer <input name="frase" size="50"
           value="acepto los términos y condiciones de mi crédito"></label></p>
        <p><label>Foto de la INE <input type="file" name="ine" accept="image/*" required></label></p>
        <fieldset style="border:1px solid #ddd;border-radius:8px;margin:16px 0">
          <legend>Marca en la pantalla (opcional; vacío = colores genéricos)</legend>
          <p><label>Logo <input type="file" name="logo" accept="image/png,image/jpeg,image/webp"></label></p>
          <p><label>Color primario <input name="color_primario" placeholder="#1B56D6" size="9"
               pattern="#?([0-9a-fA-F]{{3}}|[0-9a-fA-F]{{6}})"></label>
             <input type="color" title="Elegir" oninput="this.form.color_primario.value=this.value"></p>
          <p><label>Color secundario <input name="color_secundario" placeholder="#17864A" size="9"
               pattern="#?([0-9a-fA-F]{{3}}|[0-9a-fA-F]{{6}})"></label>
             <input type="color" title="Elegir" oninput="this.form.color_secundario.value=this.value"></p>
        </fieldset>
        <button>Continuar a la prueba de vida</button>
      </form>""")


@app.post("/iniciar")
async def iniciar(
    ine: UploadFile = File(...),
    referencia: str = Form(""),
    frase: str = Form(...),
    logo: UploadFile | None = File(None),
    color_primario: str = Form(""),
    color_secundario: str = Form(""),
):
    archivos = {"ine_imagen": (ine.filename or "ine.jpg", await ine.read(), ine.content_type or "image/jpeg")}
    if logo is not None and logo.filename:
        archivos["logo"] = (logo.filename, await logo.read(), logo.content_type or "image/png")
    datos = {
        "frase": frase,
        "referencia_externa": referencia,
        "url_retorno": f"{AQUI}/regreso",
        "metadata": json.dumps({"origen": "proyecto_demo"}),
    }
    # Solo se mandan si se llenaron; sin ellos la pantalla usa los colores genericos.
    for campo, valor in (("color_primario", color_primario), ("color_secundario", color_secundario)):
        if valor.strip():
            datos[campo] = valor.strip()
    async with httpx.AsyncClient(timeout=30) as cliente:
        r = await cliente.post(f"{VIDA_URL}/api/v1/sesiones", headers={"X-API-Key": API_KEY}, files=archivos, data=datos)
    if r.status_code != 201:
        raise HTTPException(r.status_code, r.text)
    return RedirectResponse(r.json()["url_captura"], status_code=303)


@app.post("/webhooks/vida")
async def webhook(request: Request):
    cuerpo = await request.body()  # cuerpo crudo: la firma se calcula sobre estos bytes
    try:
        partes = dict(p.split("=", 1) for p in request.headers.get("X-Vida-Firma", "").split(","))
        t = int(partes["t"])
    except (KeyError, ValueError):
        raise HTTPException(401)
    esperado = hmac.new(SECRETO.encode(), f"{t}.".encode() + cuerpo, hashlib.sha256).hexdigest()
    if abs(time.time() - t) > 300 or not hmac.compare_digest(esperado, partes.get("v1", "")):
        raise HTTPException(401)

    evento = json.loads(cuerpo)
    resultados[evento["datos"]["sesion"]["id"]] = evento
    return {"ok": True}  # 2xx = "ya lo guarde"


@app.get("/regreso", response_class=HTMLResponse)
def regreso(sesion_id: str, estado: str = "", aprobada: str = "0"):
    evento = resultados.get(sesion_id)
    if not evento:
        return _PAGINA.format(f"<p>Sesión {html.escape(sesion_id)}: {html.escape(estado)}. Aún no llega el webhook.</p>")
    resumen = evento["datos"].get("resumen") or {}
    filas = "".join(
        f"<tr><td>{html.escape(grupo)}</td><td><code>{html.escape(json.dumps(valores, ensure_ascii=False))}</code></td></tr>"
        for grupo, valores in resumen.items() if isinstance(valores, dict)
    )
    return _PAGINA.format(f"""
      <h2>{"✅ Aprobado" if aprobada == "1" else "❌ No aprobado"}</h2>
      <table style="font-size:13px">{filas}</table>
      <details><summary>JSON recibido en el webhook</summary>
      <pre style="white-space:pre-wrap;font-size:12px">{html.escape(json.dumps(evento, indent=2, ensure_ascii=False))}</pre></details>
      <p><a href="/">Otra prueba</a></p>""")

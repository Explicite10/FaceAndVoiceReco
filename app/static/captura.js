// Captura de la prueba de vida para una sesion (/captura/<token>).
//
// Flujo: inicio -> camara (encuadre; el usuario presiona "Empezar" y, tras una
// cuenta 3-2-1, una sola grabacion con gestos y frase) -> verificando ->
// resultado. Si un intento no pasa y quedan intentos, se repite con un reto
// nuevo. MediaPipe en el navegador solo guia al usuario: el servidor vuelve a
// revisar todo en el video. Mientras algo carga, un cargador cubre la pantalla.

import { analizar, cargar } from "./detector-core.js";

const $ = (id) => document.getElementById(id);
const token = location.pathname.split("/").filter(Boolean).pop();
const API = `/api/v1/captura/${encodeURIComponent(token)}`;
const ds = document.body.dataset;
const MIN_DUR = Number(ds.min);
const MAX_DUR = Number(ds.max);
// Mismos umbrales que app/services/gestures.py.
const TH = {
  turn: Number(ds.turn), closed: Number(ds.blinkClosed), open: Number(ds.blinkOpen),
  turnFrames: 3, handFrames: 3, faceFrames: 5,
};
const EN_IFRAME = window.parent !== window;
const HINTS = {
  turn_left: "Gira despacio, un poco más.",
  turn_right: "Gira despacio, un poco más.",
  blink: "Cierra los ojos un momento y ábrelos.",
  raise_left_hand: "Muestra la palma a un lado de tu cara.",
  raise_right_hand: "Muestra la palma a un lado de tu cara.",
};
const ICON_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
const ICON_BAD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 7v6M12 17h.01"/></svg>';

const SCREENS = ["sInicio", "sCamara", "sResultado"];
const show = (id) => SCREENS.forEach((s) => { $(s).hidden = s !== id; });

// Cargador a pantalla completa: atrapa los toques hasta que se quite.
function cargando(titulo, texto = "") {
  $("cargadorTitulo").textContent = titulo;
  $("cargadorTexto").textContent = texto;
  $("cargador").hidden = false;
  document.body.classList.add("bloqueado");
}
function listo() {
  $("cargador").hidden = true;
  document.body.classList.remove("bloqueado");
}
function verificando() {
  show(null);
  cargando("Verificando tu identidad", "Esto puede tardar hasta un minuto. No cierres esta ventana.");
}

let stream = null;
// det: { analizar(video, t, conManos) -> Promise<{f, hands} | null>, enPagina } o null sin detector.
let det = null, detector = null;
// Ultima grabacion, solo en memoria del navegador: se muestra si el intento no pasa.
let repeticionUrl = null;

// ---------------------------------------------------------------- servidor

async function api(path = "", opts = {}) {
  const res = await fetch(API + path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* sin cuerpo */ }
  if (!res.ok) {
    const err = new Error(body?.detail || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---------------------------------------------------------------- detector

// En un Worker la deteccion no compite con el video por el hilo de la pagina:
// en equipos lentos la camara se veia trabada. Si el navegador no puede, corre
// en la pagina con un ritmo que se adapta a lo que tarde cada cuadro.
function detectorEnWorker() {
  return new Promise((resolve) => {
    let w;
    try { w = new Worker("/static/detector.js"); } catch { resolve(null); return; }
    const pendientes = new Map();
    let n = 0;
    const fallar = () => { clearTimeout(tope); w.terminate(); resolve(null); };
    const tope = setTimeout(fallar, 30000);
    w.onerror = fallar;
    w.onmessage = (e) => {
      const m = e.data;
      if (m.tipo === "listo") {
        if (!m.ok) { fallar(); return; }
        clearTimeout(tope);
        resolve({ analizar: enviar, enPagina: false });
      } else if (m.tipo === "res") {
        pendientes.get(m.id)?.(m.f === undefined ? null : { f: m.f, hands: m.hands });
        pendientes.delete(m.id);
      }
    };
    async function enviar(video, t, conManos) {
      let bitmap;
      try { bitmap = await createImageBitmap(video); } catch { return null; }
      const id = ++n;
      const res = new Promise((r) => pendientes.set(id, r));
      w.postMessage({ tipo: "cuadro", id, bitmap, t, manos: conManos }, [bitmap]);
      // Si el Worker no contesta, no se queda esperando para siempre.
      return Promise.race([res, esperarMs(3000).then(() => { pendientes.delete(id); return null; })]);
    }
    w.postMessage({ tipo: "init" });
  });
}

async function detectorEnPagina() {
  const tareas = await cargar();
  return { analizar: async (video, t, conManos) => analizar(tareas, video, t, conManos), enPagina: true };
}

async function loadDetector() {
  try {
    det = (window.Worker && window.createImageBitmap ? await detectorEnWorker() : null) || await detectorEnPagina();
  } catch {
    // Sin detector se guia por tiempo; el servidor valida igual.
    det = null;
  }
}

// ---------------------------------------------------------------- arranque

async function arrancar() {
  let config;
  try {
    config = await api();
  } catch {
    resultado(false, "Este enlace no es válido o ya venció", "Vuelve a iniciar desde el sitio donde comenzaste tu trámite.");
    return;
  }
  detector = loadDetector();
  listo();
  ruta(config, false);
}

function ruta(c, despuesDeIntento) {
  switch (c.estado) {
    case "pendiente":
      return despuesDeIntento ? reintento(c) : inicio(c);
    case "procesando":
    case "entregando":
      verificando();
      return esperar();
    case "error_entrega":
      return errorEntrega();
    default:
      return terminado(c);
  }
}

function inicio(c) {
  if (c.intentos > 0) {
    const aviso = $("aviso");
    aviso.replaceChildren();
    const titulo = document.createElement("p");
    titulo.textContent = `Tu intento anterior no pasó. Te quedan ${c.intentos_restantes} de ${c.intentos + c.intentos_restantes}.`;
    const ul = document.createElement("ul");
    ul.className = "reasons";
    llenarMotivos(ul, c.motivos);
    aviso.append(titulo, ul);
    aviso.hidden = false;
  }
  // En un reintento ya conoce el proceso: se muestra lo que fallo y el boton, sin la explicacion.
  $("sInicio").classList.toggle("repite", c.intentos > 0);
  $("btnComenzar").onclick = iniciarIntento;
  show("sInicio");
}

// ---------------------------------------------------------------- intento

// La camara y el microfono se abren nuevos en cada intento y se cierran al terminar de
// grabar: reutilizarlos fallaba en algunos telefonos (al reproducir la grabacion con
// sonido, el sistema silenciaba o cortaba el microfono y el siguiente intento salia sin audio).
function cerrarCamara() {
  stream?.getTracks().forEach((t) => t.stop());
  stream = null;
}

// "ok" o el motivo por el que no se pudo: permiso, ocupado, dispositivo, microfono, error.
async function abrirCamara() {
  cerrarCamara();
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: "user", width: { ideal: 640 }, height: { ideal: 480 } },
      audio: { echoCancellation: true, noiseSuppression: true },
    });
  } catch (e) {
    stream = null;
    if (e?.name === "NotAllowedError" || e?.name === "SecurityError") return "permiso";
    if (e?.name === "NotReadableError" || e?.name === "AbortError") return "ocupado";
    if (e?.name === "NotFoundError" || e?.name === "OverconstrainedError") return "dispositivo";
    return "error";
  }
  const audio = stream.getAudioTracks()[0];
  if (!audio || audio.readyState !== "live") { cerrarCamara(); return "microfono"; }
  return "ok";
}

const ERROR_CAMARA = {
  permiso: "Permite el acceso a la cámara y al micrófono en tu navegador (en el ícono junto a la dirección de la página) y vuelve a intentar.",
  ocupado: "Otra aplicación está usando la cámara o el micrófono. Ciérrala (llamadas, grabadora, videollamadas) y vuelve a intentar.",
  dispositivo: "No encontramos cámara o micrófono en este dispositivo.",
  microfono: "No se pudo activar el micrófono. Revisa que el navegador tenga permiso de micrófono y que ninguna otra app lo esté usando.",
  error: "No pudimos activar la cámara y el micrófono. Vuelve a intentar.",
};

async function iniciarIntento() {
  $("btnComenzar").disabled = true;
  cargando("Preparando tu cámara", "Si tu navegador lo pide, permite el acceso a la cámara y al micrófono.");
  try {
    const camara = await abrirCamara();
    if (camara !== "ok") {
      resultado(false, "Necesitamos tu cámara y micrófono", ERROR_CAMARA[camara], "Reintentar", iniciarIntento);
      return;
    }
    let reto;
    try {
      reto = await api("/reto", { method: "POST" });
    } catch (e) {
      cerrarCamara();
      if (e.status === 409) { listo(); return ruta(await api(), true); }
      resultado(false, "No pudimos iniciar", e.message, "Reintentar", iniciarIntento);
      return;
    }
    await grabar(reto);
  } finally {
    $("btnComenzar").disabled = false;
  }
}

const esperarMs = (ms) => new Promise((r) => setTimeout(r, ms));
const siguienteCuadro = (video) => Promise.race([
  new Promise((r) => (video.requestVideoFrameCallback ? video.requestVideoFrameCallback(() => r()) : requestAnimationFrame(() => r()))),
  esperarMs(250),
]);

// Muchas camaras entregan cuadros negros el primer par de segundos (sobre todo
// la primera vez en un equipo). Se espera a ver imagen de verdad: varios cuadros
// seguidos con algo de brillo. Si el cuarto esta muy oscuro, se sigue al tope.
async function camaraLista(video) {
  try { await video.play(); } catch { /* autoplay muted no deberia fallar */ }
  const lienzo = document.createElement("canvas");
  lienzo.width = 32; lienzo.height = 24;
  const ctx = lienzo.getContext("2d", { willReadFrequently: true });
  const tope = performance.now() + 8000;
  let buenos = 0;
  while (performance.now() < tope) {
    await siguienteCuadro(video);
    if (video.readyState < 2 || !video.videoWidth) continue;
    ctx.drawImage(video, 0, 0, lienzo.width, lienzo.height);
    const d = ctx.getImageData(0, 0, lienzo.width, lienzo.height).data;
    let suma = 0;
    for (let i = 0; i < d.length; i += 4) suma += d[i] + d[i + 1] + d[i + 2];
    buenos = suma / (d.length / 4 * 3) > 14 ? buenos + 1 : 0;
    if (buenos >= 5) return true;
  }
  return false;
}

// Lienzo que copia la vista de la camara, ya derecha (el navegador aplica la rotacion
// del sensor al mostrarla). Devuelve { pista, detener } o null si no se puede: entonces
// se graba la camara directo y el servidor endereza el video si hace falta.
function crearLienzo(video) {
  const w0 = video.videoWidth, h0 = video.videoHeight;
  if (!w0 || !h0 || !HTMLCanvasElement.prototype.captureStream) return null;
  const escala = Math.min(1, 640 / Math.max(w0, h0));  // como maximo 640 px de lado
  const lienzo = document.createElement("canvas");
  lienzo.width = Math.round(w0 * escala);
  lienzo.height = Math.round(h0 * escala);
  const ctx = lienzo.getContext("2d", { alpha: false });
  let pista;
  try {
    ctx.drawImage(video, 0, 0, lienzo.width, lienzo.height);
    pista = lienzo.captureStream(30).getVideoTracks();
  } catch {
    return null;
  }
  if (!pista.length) return null;
  // Sin espejo: se dibujan los pixeles tal cual (el espejo de la vista es solo CSS).
  const reloj = setInterval(() => {
    if (video.readyState >= 2) ctx.drawImage(video, 0, 0, lienzo.width, lienzo.height);
  }, 1000 / 30);
  return {
    pista,
    detener() { clearInterval(reloj); pista.forEach((t) => t.stop()); },
  };
}

function pickMime() {
  const opts = ["video/webm;codecs=vp8,opus", "video/webm", "video/mp4"];
  return opts.find((o) => window.MediaRecorder && MediaRecorder.isTypeSupported(o)) || "";
}

// Iconos de linea (stroke = currentColor), uno por tipo de paso.
const SVG = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
const ICONOS = {
  rostro: SVG('<circle cx="12" cy="12" r="9"/><path d="M9 10h.01M15 10h.01M9 15c1.7 1.3 4.3 1.3 6 0"/>'),
  turn_left: SVG('<circle cx="13" cy="12" r="7"/><path d="M8 5L3 9l5 4"/><path d="M3 9h7"/>'),
  turn_right: SVG('<circle cx="11" cy="12" r="7"/><path d="M16 5l5 4-5 4"/><path d="M21 9h-7"/>'),
  blink: SVG('<path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.5"/>'),
  raise_left_hand: SVG('<path d="M8 13V6.5a1.5 1.5 0 013 0V12M11 11V4.5a1.5 1.5 0 013 0V11M14 11V6a1.5 1.5 0 013 0v7"/><path d="M17 13a1.5 1.5 0 013 0v1.5A7.5 7.5 0 0112.5 22h-.8a6.5 6.5 0 01-5.4-2.9l-2.6-3.9a1.5 1.5 0 012.4-1.8L8 15"/>'),
  frase: SVG('<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0014 0M12 18v3"/>'),
};
ICONOS.raise_right_hand = ICONOS.raise_left_hand;
const ETIQUETAS = {
  turn_left: "Girar", turn_right: "Girar", blink: "Parpadear",
  raise_left_hand: "Mano", raise_right_hand: "Mano", frase: "Frase",
};

const mmss = (ms) => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };

async function grabar(reto) {
  const gestos = reto.gestures;
  const total = gestos.length + 1;  // gestos + frase
  const video = $("video");
  video.srcObject = stream;

  // La vista previa toma la proporcion real de la camara (4:3, 16:9, vertical...):
  // se ve el encuadre completo que se graba y se analiza, sin recortes.
  const ajustarProporcion = () => {
    if (video.videoWidth && video.videoHeight) {
      $("stage").style.setProperty("--cam-ratio", (video.videoWidth / video.videoHeight).toFixed(4));
    }
  };
  video.onloadedmetadata = ajustarProporcion;
  video.onresize = ajustarProporcion;  // p. ej. al girar el telefono
  ajustarProporcion();

  const pasos = [...gestos.map((g) => g.gesture), "frase"];
  $("progreso").innerHTML = pasos.map((p) =>
    `<li>${ICONOS[p]}<span>${ETIQUETAS[p]}</span><span class="estado"></span></li>`).join("");
  const items = [...$("progreso").children];

  const mostrarPaso = (icono, texto, pista = "", animar = false) => {
    $("pasoIcono").innerHTML = ICONOS[icono];
    $("pasoIcono").classList.toggle("go", animar);
    $("instruccion").textContent = texto;
    $("pista").textContent = pista;
  };
  mostrarPaso("rostro", "Coloca tu rostro frente a la cámara", "Cuando estés listo, presiona Empezar grabación.");
  $("pasoN").textContent = "Prepárate";
  $("fraseBox").hidden = true;
  $("btnListo").hidden = true;
  $("rec").hidden = true;
  $("bien").hidden = true;
  $("cuentaAtras").hidden = true;
  $("sCamara").classList.remove("leyendo");
  const btnGrabar = $("btnGrabar");
  btnGrabar.hidden = false;
  btnGrabar.disabled = true;
  btnGrabar.textContent = "Empezar grabación";
  // La pantalla se arma debajo del cargador: asi el video ya se esta pintando.
  show("sCamara");
  const [hayImagen] = await Promise.all([
    camaraLista(video),
    // El detector carga desde que abre la pagina; no se espera de mas.
    Promise.race([detector, esperarMs(6000)]),
  ]);
  if (!hayImagen) $("pista").textContent = "Se ve muy oscuro. Busca más luz para que te veamos bien.";
  listo();

  // Se graba lo que se ve, ya derecho: cada cuadro de la camara se dibuja en un lienzo
  // y se graba el lienzo con el audio del microfono. Asi el video sale derecho en
  // cualquier celular (algunos, sobre todo iPhone, grababan la imagen "acostada").
  const enLienzo = crearLienzo(video);
  const streamGrabacion = enLienzo ? new MediaStream([...enLienzo.pista, ...stream.getAudioTracks()]) : stream;

  const mime = pickMime();
  const chunks = [];
  // 1.2 Mbps sobra para 640x480 y aligera la codificacion y la subida.
  let recorder;
  try {
    recorder = new MediaRecorder(streamGrabacion, { ...(mime ? { mimeType: mime } : {}), videoBitsPerSecond: 1_200_000 });
  } catch {
    // Algunos navegadores rechazan el formato o la tasa: se deja que elijan.
    recorder = new MediaRecorder(streamGrabacion);
  }
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  // Pedazos de 1 s solo en WebM (Chrome, Android); en Safari (MP4) una sola pieza.
  const enPedazos = /webm/i.test(recorder.mimeType || mime);

  // Vigilancia del microfono: si el sistema lo corta o lo silencia a media grabacion,
  // el video saldria sin audio y el servidor lo rechazaria (gastando un intento).
  const streamIntento = stream;
  const pistaAudio = stream.getAudioTracks()[0];
  let audioTerminado = false, mudoDesde = null, mudoMs = 0;
  pistaAudio.onended = () => { if (inicioRec) audioTerminado = true; };
  pistaAudio.onmute = () => { if (inicioRec && mudoDesde === null) mudoDesde = performance.now(); };
  pistaAudio.onunmute = () => { if (mudoDesde !== null) { mudoMs += performance.now() - mudoDesde; mudoDesde = null; } };

  // fase: encuadre -> cuenta -> gesto -> frase -> fin
  let fase = "encuadre", paso = 0, desde = performance.now(), inicioRec = 0, ultimo = 0;
  // Segundos del video en que se mostro la frase y en que termino: el servidor
  // solo escucha ese tramo, asi lo dicho durante los gestos no cuenta.
  let fraseInicio = null, fraseFin = null;
  let ocupado = false, intervalo = 66;  // ~15 cuadros por segundo como maximo
  let run = 0, faceRun = 0, seenOpen = false, closed = false, lastFace = null, fraseTimer = null;

  const marcar = () => items.forEach((li, i) => {
    li.className = i < paso ? "done" : i === paso ? "current" : "";
    li.querySelector(".estado").textContent = i < paso ? "Listo" : i === paso ? "Ahora" : "";
  });

  function celebrar() {
    const b = $("bien");
    b.hidden = true;
    void b.offsetWidth;  // reinicia la animacion
    b.hidden = false;
    setTimeout(() => { b.hidden = true; }, 700);
  }

  function siguientePaso() {
    if (paso >= 0 && fase === "gesto") celebrar();
    paso += 1; run = 0; seenOpen = false; closed = false; desde = performance.now();
    marcar();
    $("pasoN").textContent = `Paso ${paso + 1} de ${total}`;
    if (paso < gestos.length) {
      mostrarPaso(gestos[paso].gesture, gestos[paso].instruction, "", true);
      return;
    }
    fase = "frase";
    fraseInicio = (performance.now() - inicioRec) / 1000;
    $("sCamara").classList.add("leyendo");
    mostrarPaso("frase", "Ahora lee la frase en voz alta", "Mirando a la cámara, con voz clara.");
    $("frase").textContent = reto.phrase;
    // Frases largas (consentimientos): letra mas chica para que quepan con el boton a la vista.
    $("frase").classList.toggle("larga", reto.phrase.split(/\s+/).length > 25);
    $("fraseBox").hidden = false;
    $("btnListo").hidden = false;
    $("btnListo").disabled = true;
    setTimeout(() => { $("btnListo").disabled = false; }, 2500);
    // Tiempo para leer: ~0.5 s por palabra mas margen (la frase la define el proyecto, hasta 50 palabras).
    const segundos = Math.min(30, Math.max(6, reto.phrase.split(/\s+/).length * 0.5 + 3));
    const barra = $("cuenta");
    barra.style.transition = "none";
    barra.style.transform = "scaleX(1)";
    void barra.offsetWidth;
    barra.style.transition = `transform ${segundos}s linear`;
    barra.style.transform = "scaleX(0)";
    fraseTimer = setTimeout(terminar, segundos * 1000);
  }

  function empezarGrabacion() {
    // En pedazos de 1 s (WebM): el video se va juntando mientras se graba y no
    // depende de un solo envio al final (que algunos navegadores no hacen).
    recorder.start(enPedazos ? 1000 : undefined);
    inicioRec = performance.now();
    $("rec").hidden = false;
    fase = "gesto";
    paso = -1;
    siguientePaso();
  }

  // El usuario decide cuando empieza; una cuenta corta le da tiempo de acomodarse.
  btnGrabar.onclick = () => {
    if (fase !== "encuadre") return;
    fase = "cuenta";
    btnGrabar.hidden = true;
    const numero = $("cuentaAtras");
    let n = 3;
    const tic = () => {
      if (n === 0) { numero.hidden = true; empezarGrabacion(); return; }
      numero.textContent = n;
      numero.hidden = false;
      numero.classList.remove("tic");
      void numero.offsetWidth;  // reinicia la animacion
      numero.classList.add("tic");
      n -= 1;
      setTimeout(tic, 900);
    };
    tic();
  };

  // Solo deja empezar con el rostro a la vista (sin detector, de inmediato).
  function encuadre(veRostro) {
    const ok = !det || veRostro;
    btnGrabar.disabled = !ok;
    $("instruccion").textContent = ok ? "Todo listo para empezar" : "Coloca tu rostro frente a la cámara";
  }

  function cumplido(gesto, f, hands) {
    if (gesto === "turn_left" || gesto === "turn_right") {
      const ok = f && f.turn != null && (gesto === "turn_left" ? f.turn > TH.turn : f.turn < -TH.turn);
      run = ok ? run + 1 : 0;
      return run >= TH.turnFrames;
    }
    if (gesto === "blink") {
      if (!f) return false;
      if (f.blink <= TH.open) { if (closed) return true; seenOpen = true; }
      else if (f.blink >= TH.closed && seenOpen) closed = true;
      return false;
    }
    // Alzar mano: palma arriba de la barbilla, del lado pedido de la nariz.
    if (!lastFace) return false;
    const izquierda = gesto === "raise_left_hand";
    const ok = (hands || []).some((p) =>
      (izquierda ? p.x > lastFace.noseX : p.x < lastFace.noseX) && p.y < lastFace.chinY);
    run = ok ? run + 1 : 0;
    return run >= TH.handFrames;
  }

  function terminar() {
    if (fase === "fin") return;
    if (fase === "frase") fraseFin = (performance.now() - inicioRec) / 1000;
    fase = "fin";
    clearTimeout(fraseTimer);
    $("btnListo").hidden = true;
    $("pasoN").textContent = "Guardando…";
    items.forEach((li) => { li.className = "done"; li.querySelector(".estado").textContent = "Listo"; });
    const falta = MIN_DUR * 1000 - (performance.now() - inicioRec);
    setTimeout(() => {
      try {
        if (recorder.state !== "inactive") {
          recorder.requestData();  // lo que tenga pendiente, por si stop no lo entrega
          recorder.stop();
        }
      } catch { /* ya se habia detenido */ }
      // Si el navegador no avisa que termino (onstop), se arma con lo que ya se junto:
      // asi no se queda en "Guardando…" para siempre.
      vigia = setTimeout(() => {
        try { if (recorder.state !== "inactive") recorder.requestData(); } catch { /* sin datos pendientes */ }
        setTimeout(finalizar, 500);
      }, 3000);
    }, Math.max(0, falta));
  }
  $("btnListo").onclick = terminar;

  // Una sola vez, venga de onstop, del vigia, de un error de la grabadora o de la camara.
  let finalizado = false, vigia = null;
  function finalizar(motivo = null) {
    if (finalizado) return;
    finalizado = true;
    clearTimeout(vigia);
    clearTimeout(fraseTimer);
    fase = "fin";
    $("rec").hidden = true;
    // La camara y el microfono se apagan ya: asi la repeticion con sonido no
    // interfiere con el siguiente intento.
    if (mudoDesde !== null) mudoMs += performance.now() - mudoDesde;
    const audioCortado = audioTerminado || mudoMs > 1500;
    enLienzo?.detener();
    if (stream === streamIntento) cerrarCamara();

    if (!chunks.length || motivo) {
      resultado(false, "No pudimos guardar la grabación",
        motivo || "Vuelve a intentarlo; este intento no se cuenta.", "Intentar de nuevo", iniciarIntento);
      return;
    }
    const tipo = recorder.mimeType || mime;
    const blob = new Blob(chunks, { type: tipo || "video/webm" });
    if (repeticionUrl) URL.revokeObjectURL(repeticionUrl);
    repeticionUrl = URL.createObjectURL(blob);
    if (audioCortado) {
      // Sin voz el servidor lo rechazaria: no se envia y no se gasta el intento.
      resultado(false, "No se grabó tu voz",
        "El micrófono se desactivó durante la grabación. Cierra otras apps que lo usen y vuelve a intentar; este intento no se cuenta.",
        "Intentar de nuevo", iniciarIntento, [], true);
      return;
    }
    subir(blob, tipo, { fraseInicio, fraseFin });
  }
  recorder.onstop = () => finalizar();
  recorder.onerror = () => finalizar("La grabación se interrumpió. Vuelve a intentarlo; este intento no se cuenta.");
  // Si la camara se corta a media grabacion (otra app la toma, se bloquea el telefono...).
  stream.getVideoTracks().forEach((t) => {
    t.onended = () => {
      if (fase === "fin" || !inicioRec) return;
      finalizar("La cámara se desactivó durante la grabación. Vuelve a intentarlo; este intento no se cuenta.");
    };
  });

  function tick(now) {
    if (fase === "fin") return;
    if (inicioRec) {
      $("recTiempo").textContent = mmss(now - inicioRec);
      if (now - inicioRec > (MAX_DUR - 1) * 1000) { terminar(); return; }
    }

    // Solo hace falta detectar al encuadrar y en los gestos (no en la cuenta ni en la frase).
    // Un cuadro a la vez: si el equipo es lento, se analizan menos por segundo.
    const detectar = fase === "encuadre" || fase === "gesto";
    if (detectar && !ocupado && now - ultimo >= intervalo && video.readyState >= 2) {
      ultimo = now;
      const gesto = fase === "gesto" ? gestos[paso].gesture : null;
      if (!det) {
        procesar(null, null, now);
      } else {
        ocupado = true;
        const enviado = { fase, paso, t0: performance.now() };
        det.analizar(video, now, !!gesto?.startsWith("raise_")).then((r) => {
          ocupado = false;
          const dur = performance.now() - enviado.t0;
          // En la pagina se deja al menos el doble de respiro para que el video se pinte.
          intervalo = Math.min(300, Math.max(66, det.enPagina ? dur * 3 : dur));
          // Un resultado que llega tarde, de otro paso, se descarta.
          if (r && fase === enviado.fase && paso === enviado.paso) procesar(r.f, r.hands, performance.now());
        });
      }
    }
    requestAnimationFrame(tick);
  }

  function procesar(f, hands, now) {
    if (f) lastFace = f;
    if (fase === "encuadre") {
      faceRun = f ? faceRun + 1 : 0;
      encuadre(faceRun >= TH.faceFrames);
    } else if (fase === "gesto") {
      const gesto = gestos[paso].gesture;
      const listo = det ? cumplido(gesto, f, hands) : now - desde > 3500;
      if (listo) siguientePaso();
      else if (now - desde > 7000) $("pista").textContent = HINTS[gesto];
    }
  }
  requestAnimationFrame(tick);
}

async function subir(blob, mime, tiempos = {}) {
  verificando();
  const fd = new FormData();
  fd.append("video", blob, `intento.${(mime || "").includes("mp4") ? "mp4" : "webm"}`);
  if (tiempos.fraseInicio != null) fd.append("frase_inicio", tiempos.fraseInicio.toFixed(2));
  if (tiempos.fraseFin != null) fd.append("frase_fin", tiempos.fraseFin.toFixed(2));
  try {
    await api("/video", { method: "POST", body: fd });
  } catch (e) {
    if (e.status === 400) {
      resultado(false, "Se acabó el tiempo del reto", "Vamos a intentarlo con uno nuevo.", "Intentar de nuevo", iniciarIntento);
    } else if (e.status === 409) {
      ruta(await api(), true);
    } else {
      resultado(false, "No se pudo enviar el video", "Revisa tu conexión e intenta de nuevo.", "Intentar de nuevo", iniciarIntento);
    }
    return;
  }
  esperar();
}

async function esperar() {
  for (;;) {
    await esperarMs(2000);
    let c;
    try { c = await api(); } catch { continue; }
    if (c.estado !== "procesando" && c.estado !== "entregando") return ruta(c, true);
  }
}

// ---------------------------------------------------------------- finales

function llenarMotivos(ul, motivos = []) {
  ul.replaceChildren(...motivos.map((m) => Object.assign(document.createElement("li"), { textContent: m })));
  ul.hidden = motivos.length === 0;
}

function resultado(ok, titulo, texto, boton, accion, motivos = [], conRepeticion = false) {
  listo();
  $("icono").className = `badge ${ok ? "ok" : "bad"}`;
  $("icono").innerHTML = ok ? ICON_OK : ICON_BAD;
  $("resTitulo").textContent = titulo;
  llenarMotivos($("motivos"), motivos);
  $("resTexto").textContent = texto;
  const btn = $("btnAccion");
  btn.hidden = !boton;
  if (boton) {
    btn.textContent = boton;
    btn.disabled = false;
    btn.onclick = () => { btn.disabled = true; repeticion(false); accion(); };
  }
  repeticion(conRepeticion);
  show("sResultado");
}

// Muestra (o quita) la repeticion local del ultimo intento. No sale del navegador.
function repeticion(mostrar) {
  const v = $("repeticionVideo");
  const visible = mostrar && !!repeticionUrl;
  $("repeticion").hidden = !visible;
  if (visible) {
    if (v.src !== repeticionUrl) v.src = repeticionUrl;
  } else {
    v.pause();
  }
}

function reintento(c) {
  resultado(false, "No pudimos verificarte",
    `Te quedan ${c.intentos_restantes} ${c.intentos_restantes === 1 ? "intento" : "intentos"}.`,
    "Intentar de nuevo", iniciarIntento, c.motivos, true);
}

function errorEntrega() {
  resultado(false, "No se pudo registrar el resultado",
    "Tu verificación terminó, pero no pudimos enviarla. Intenta de nuevo en un momento.",
    "Reintentar envío", async () => {
      verificando();
      try { ruta(await api("/reintentar-entrega", { method: "POST" }), true); }
      catch { ruta(await api(), true); }
    });
}

function terminado(c) {
  cerrarCamara();

  if (EN_IFRAME) {
    // La pagina que incrusta la captura decide que sigue.
    window.parent.postMessage(
      { tipo: "prueba-vida:finalizada", sesion_id: c.sesion_id, estado: c.estado, aprobada: !!c.aprobada },
      c.origen_retorno || "*",
    );
  }
  const seguir = c.redireccion && !EN_IFRAME;
  // Si no paso, no se redirige solo: que pueda ver su repeticion y siga con el boton.
  const conRepeticion = c.estado === "completada" && !c.aprobada && !!repeticionUrl;
  const despues = !seguir ? "Ya puedes cerrar esta ventana."
    : conRepeticion ? "Presiona Continuar para seguir con tu trámite." : "Te regresamos en un momento…";
  const continuar = seguir ? () => location.replace(c.redireccion) : null;

  if (c.estado === "completada" && c.aprobada) {
    resultado(true, "¡Listo!", `Verificamos tu identidad. ${despues}`, seguir && "Continuar", continuar);
  } else if (c.estado === "completada") {
    resultado(false, "No fue posible verificar tu identidad",
      `Te indicarán los siguientes pasos. ${despues}`, seguir && "Continuar", continuar, c.motivos, true);
  } else if (c.estado === "expirada") {
    resultado(false, "El enlace venció", "Vuelve a iniciar desde el sitio donde comenzaste tu trámite.");
  } else {
    resultado(false, "La verificación se canceló", despues, seguir && "Continuar", continuar);
  }
  if (seguir && !conRepeticion) setTimeout(continuar, 3000);
}

arrancar();

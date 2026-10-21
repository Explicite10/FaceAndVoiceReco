// Dibuja el resultado de una verificacion (video o foto) como HTML.
// Lo usan la pagina de la prueba (al terminar) y el panel (modal "Ver registro"),
// asi los dos se ven igual. Todo queda dentro de window.Resultado para no chocar
// con las variables de cada pagina. Los estilos estan en resultado.css (.resultado).
(function () {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const SVG = (d, w = 12, sw = 3.2) => `<svg width="${w}" height="${w}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const I = {
    check: SVG('<path d="M20 6 9 17l-5-5"/>'),
    x: SVG('<path d="M18 6 6 18M6 6l12 12"/>'),
    minus: SVG('<path d="M5 12h14"/>'),
    checkBig: SVG('<path d="M20 6 9 17l-5-5"/>', 22, 3),
    alertBig: SVG('<path d="M12 7v6M12 17h.01"/>', 22, 3),
    face: SVG('<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 6-6h4a6 6 0 0 1 6 6v1"/>', 16, 2),
    hand: SVG('<path d="M18 11V6a2 2 0 0 0-4 0v5"/><path d="M14 10V4a2 2 0 0 0-4 0v6"/><path d="M10 10.5V6a2 2 0 0 0-4 0v8a8 8 0 0 0 16 0v-2a2 2 0 0 0-4 0"/>', 16, 2),
    mic: SVG('<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>', 16, 2),
    pulse: SVG('<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>', 16, 2),
    image: SVG('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21"/>', 16, 2),
    shield: SVG('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>', 28, 1.8),
  };
  const STATUS_LABEL = { passed: "Correcto", failed: "No pasó", not_evaluated: "Sin evaluar" };
  // Por que no paso, en palabras del usuario (mismos codigos que el reporte del servidor).
  const MOTIVOS = {
    id_face_not_found: "No se encontró un rostro en la identificación. Usa una foto clara del frente de la INE.",
    video_face_not_found: "No se vio el rostro en el video. Acércate a la cámara y busca buena luz.",
    face_missing_in_segment: "El rostro no se vio durante todo el video. Mantente frente a la cámara.",
    face_mismatch: "El rostro no coincide con la foto de la identificación.",
    photo_face_not_found: "No se encontró un rostro en la foto.",
    multiple_faces_detected: "Se detectó más de una persona. Debe haber una sola frente a la cámara.",
    person_changed: "Debe ser la misma persona durante todo el video.",
    audio_missing: "No se grabó el audio. Revisa el permiso del micrófono.",
    audio_duration_invalid: "La grabación fue muy corta o muy larga.",
    invalid_audio: "La grabación fue muy corta o muy larga.",
    no_speech_detected: "No se escuchó la voz. Habla más fuerte y cerca del micrófono.",
    speech_low_confidence: "No se entendió la voz. Busca un lugar sin ruido.",
    phrase_mismatch: "La frase no coincide con la que se pidió. Hay que leerla completa y en voz clara.",
    spoof_detected: "Parece una foto o una pantalla. Debe haber una persona real frente a la cámara.",
    face_not_tracked: "El rostro salió de la imagen gran parte del tiempo.",
    lips_not_moving: "No se vio movimiento de labios al hablar.",
    lips_out_of_sync: "El movimiento de los labios no coincidió con la voz.",
    head_turn_not_detected: "No se detectó el giro de cabeza.",
    blink_not_detected: "No se detectó el parpadeo.",
    hand_raise_not_detected: "No se detectó la mano alzada.",
    gesture_order_mismatch: "Los movimientos no se hicieron en el orden indicado.",
    face_too_small: "El rostro se ve muy pequeño en la foto. Acércate más.",
    image_blurry: "La foto está borrosa.",
    image_too_dark: "La foto está muy oscura.",
    image_overexposed: "La foto tiene demasiada luz.",
  };
  const MOTIVO_GENERICO = "No fue posible confirmar la identidad. Intenta de nuevo con buena luz, de frente a la cámara.";
  const GESTURE = {
    turn_left: "Girar a la izquierda", turn_right: "Girar a la derecha", blink: "Parpadear",
    raise_left_hand: "Alzar mano izquierda", raise_right_hand: "Alzar mano derecha",
  };

  const clamp = (x) => Math.max(0, Math.min(100, Number(x) || 0));
  const num = (x, d = 1) => (x == null || isNaN(x) ? "&mdash;" : `${Number(x).toFixed(d)}%`);
  const pct = (x) => (x == null ? "&mdash;" : `${Math.round(x * 100)}%`);

  function groupStatus(group) {
    const s = Object.values(group || {}).map((c) => c.status);
    if (s.includes("failed")) return "failed";
    if (s.length && s.every((x) => x === "passed")) return "passed";
    return "not_evaluated";
  }

  // Motivos de rechazo en palabras, a partir de los codigos de los checks fallidos.
  function motivosDeCodigos(codigos) {
    const out = [];
    codigos.forEach((c) => { const t = MOTIVOS[c]; if (t && !out.includes(t)) out.push(t); });
    return out.length ? out : [MOTIVO_GENERICO];
  }
  function motivos(d) {
    const codigos = [];
    Object.values(d.checks || {}).forEach((g) => Object.values(g).forEach((c) => {
      if (c.status === "failed" && c.code) codigos.push(c.code);
    }));
    return motivosDeCodigos(codigos);
  }

  function verdict(d, meta) {
    const ok = d.result.approved;
    const cuerpo = ok
      ? "<p>Todas las validaciones se cumplieron.</p>"
      : `<ul>${motivos(d).map((m) => `<li>${esc(m)}</li>`).join("")}</ul>`;
    const linea = meta ?? (d.processing_time_ms != null ? `Análisis completado en ${(d.processing_time_ms / 1000).toFixed(1)} s` : "");
    return `<div class="verdict verdict--${ok ? "ok" : "bad"}">
        <div class="verdict-icon">${ok ? I.checkBig : I.alertBig}</div>
        <div style="min-width:0">
          <h3>${ok ? "Identidad verificada" : "No se pudo verificar la identidad"}</h3>
          ${cuerpo}
          ${linea ? `<div class="meta">${esc(linea)}</div>` : ""}
        </div>
      </div>`;
  }

  const tiles = (list, cls) => `<div class="tiles ${cls}">` + list.map((t) =>
    `<div class="tile ${t.status}"><small>${t.label}</small><strong>${t.value}</strong></div>`).join("") + "</div>";

  function panel(title, icon, group, body) {
    const st = groupStatus(group);
    return `<details class="panel" open><summary><span class="panel-title">${icon}${title}</span>
      <span class="pill pill--${st}">${STATUS_LABEL[st]}</span></summary><div class="panel-body">${body}</div></details>`;
  }

  function meterRow(label, value, threshold, ok, text) {
    const thr = threshold != null ? `<i class="bar-thr" style="left:${clamp(threshold)}%" title="Mínimo"></i>` : "";
    const cls = ok == null ? "" : ok ? "ok" : "bad";
    return `<div class="meter-row"><span>${label}</span>
      <div class="bar"><span class="bar-fill ${cls}" style="width:${clamp(value)}%"></span>${thr}</div>
      <b class="${cls}">${text}</b></div>`;
  }

  const kv = (rows) => `<dl class="kv">${rows.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`;
  const ICON = { passed: I.check, failed: I.x, not_evaluated: I.minus };
  const renderChecks = (group) => "<ul class='checks'>" + Object.values(group || {}).map((c) =>
    `<li class="${c.status}"><span class="ic">${ICON[c.status]}</span><span>${esc(c.message)}</span></li>`).join("") + "</ul>";

  // ---------------------------------------------------------------- video

  function renderFace(checks, f) {
    let html = renderChecks(checks);
    if (!f) return html;
    const thr = f.similarity.threshold_percent;
    const frase = f.segments.find((s) => s.role === "frase");
    if (frase) {
      // La INE se compara contra la cara mientras leia la frase (de frente y quieto);
      // en los movimientos se revisa que sea la misma persona que leyo.
      const sp = f.same_person || {};
      const gestos = f.segments.filter((s) => s.role !== "frase").map((s, i) =>
        meterRow(`Movimientos ${i + 1}`, s.same_person_percent ?? 0, sp.threshold_percent, s.face_detected ? s.match : false,
          s.face_detected && s.same_person_percent != null ? `${s.same_person_percent.toFixed(1)}%` : "sin rostro")).join("");
      return html + `
        <div class="sub">Rostro contra la INE (mientras leía)</div>
        <div class="big">${num(f.similarity.average_percent, 2)}<small>mínimo ${thr}%</small></div>
        <div class="meter" style="margin-top:8px">${meterRow("Similitud", frase.similarity_percent ?? 0, thr, frase.match, num(frase.similarity_percent))}</div>
        <div class="sub">Misma persona durante los movimientos</div>
        <div class="meter">${gestos}</div>
        <p class="hint">La línea vertical marca el mínimo requerido.</p>`;
    }
    const segs = f.segments.map((s) => meterRow(`Tramo ${s.index}`, s.face_detected ? s.similarity_percent : 0, thr, s.match,
      s.face_detected ? `${s.similarity_percent.toFixed(1)}%` : "sin rostro")).join("");
    return html + `
      <div class="sub">Similitud promedio</div>
      <div class="big">${num(f.similarity.average_percent, 2)}<small>mínimo por tramo ${thr}%</small></div>
      <div class="sub">Por tramo del video</div>
      <div class="meter">${segs}</div>
      <p class="hint">La línea vertical marca el mínimo requerido.</p>`;
  }

  function renderVoice(checks, v) {
    let html = renderChecks(checks);
    if (!v) return html;
    const words = (arr) => arr.length
      ? `<span class="words">${arr.map((w) => `<span class="word">${esc(w)}</span>`).join("")}</span>`
      : "<span class='none'>ninguna</span>";
    return html + `
      <div class="sub">Coincidencia de la frase</div>
      <div class="big">${num(v.phrase_match.match_percent, 1)}</div>
      <div class="compare" style="margin-top:10px">
        <div class="quote"><small>Frase pedida</small>${esc(v.expected_phrase)}</div>
        <div class="quote"><small>Se escuchó</small><b>${esc(v.transcript) || "(nada)"}</b></div>
      </div>
      <div style="margin-top:8px">${kv([
        ["Palabras faltantes", words(v.phrase_match.missing_words)],
        ["Palabras de más", words(v.phrase_match.extra_words)],
      ])}</div>`;
  }

  function renderLiveness(checks, l) {
    let html = renderChecks(checks);
    if (!l) return html;
    const a = l.anti_spoofing;
    const segs = (a.by_segment_probability || []).map((x, i) =>
      meterRow(`Tramo ${i + 1}`, (x ?? 0) * 100, null, null, pct(x))).join("");
    return html + `
      <div class="sub">Probabilidad de persona real</div>
      <div class="big">${pct(a.real_probability)}</div>
      <div class="meter" style="margin-top:8px">${segs}</div>`;
  }

  function renderChallenge(checks, c) {
    let html = renderChecks(checks);
    if (!c) return html;
    const rows = c.gestures.map((g, i) => `<li><span>${i + 1}. ${GESTURE[g.gesture] || ""}</span>
      <b class="${g.detected ? "ok" : "bad"}">${g.detected ? `✓ segundo ${g.time_sec}` : "✗ no detectado"}</b></li>`).join("");
    return html + `<div class="sub">Orden del reto</div><ol class="timeline">${rows}</ol>`;
  }

  function video(d, meta) {
    const f = d.details.face, v = d.details.voice, l = d.details.liveness, c = d.details.challenge;
    return verdict(d, meta) + tiles([
      { label: "Rostro vs. INE", value: num(f?.similarity?.average_percent), status: groupStatus(d.checks.face) },
      { label: "Movimientos", value: c?.gestures ? `${c.gestures.filter((g) => g.detected).length}/${c.gestures.length}` : "&mdash;", status: groupStatus(d.checks.challenge) },
      { label: "Frase", value: num(v?.phrase_match?.match_percent), status: groupStatus(d.checks.voice) },
      { label: "Persona real", value: pct(l?.anti_spoofing?.real_probability), status: groupStatus(d.checks.liveness) },
    ], "four") +
      panel("Rostro", I.face, d.checks.face, renderFace(d.checks.face, f)) +
      panel("Movimientos del reto", I.hand, d.checks.challenge, renderChallenge(d.checks.challenge, c)) +
      panel("Frase hablada", I.mic, d.checks.voice, renderVoice(d.checks.voice, v)) +
      panel("Prueba de vida", I.pulse, d.checks.liveness, renderLiveness(d.checks.liveness, l));
  }

  // ---------------------------------------------------------------- foto

  function foto(d, meta) {
    const det = d.details;
    const sim = det.similarity;
    const match = d.checks.face.face_match?.status === "passed";
    const a = det.anti_spoofing;
    return verdict(d, meta) + tiles([
      { label: "Rostro vs. INE", value: sim ? `${sim.percent.toFixed(1)}%` : "&mdash;", status: groupStatus(d.checks.face) },
      { label: "Calidad", value: STATUS_LABEL[groupStatus(d.checks.quality)], status: groupStatus(d.checks.quality) },
      { label: "Persona real", value: pct(a?.real_probability), status: groupStatus(d.checks.liveness) },
    ], "three") +
      panel("Rostro", I.face, d.checks.face, renderChecks(d.checks.face) +
        (sim ? `<div class="sub">Similitud con la INE</div>
          <div class="big ${match ? "ok" : "bad"}">${sim.percent.toFixed(2)}%<small>mínimo ${sim.threshold_percent}%</small></div>
          <div class="meter" style="margin-top:8px">${meterRow("Similitud", sim.percent, sim.threshold_percent, match, `${sim.percent.toFixed(1)}%`)}</div>
          <p class="hint">La línea vertical marca el mínimo requerido.</p>` : "")) +
      panel("Calidad de la foto", I.image, d.checks.quality, renderChecks(d.checks.quality)) +
      panel("Prueba de vida", I.pulse, d.checks.liveness, renderChecks(d.checks.liveness) +
        (a ? `<div class="sub">Probabilidad de persona real</div>
          <div class="meter">${meterRow("Persona real", (a.real_probability ?? 0) * 100, (a.threshold_probability ?? 0) * 100,
            a.real_probability >= a.threshold_probability, pct(a.real_probability))}</div>` : ""));
  }

  window.Resultado = { video, foto, verdict, motivosDeCodigos, I, STATUS_LABEL, esc };
})();

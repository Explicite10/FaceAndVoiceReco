// Worker del detector: MediaPipe corre aqui y no en la pagina, asi la vista
// previa de la camara no se traba en equipos lentos. La pagina manda un cuadro
// a la vez (ImageBitmap) y espera la respuesta antes de mandar el siguiente.
//
// Es un Worker clasico (no "module") a proposito: MediaPipe carga su WASM con
// importScripts, que no existe en los Workers de modulo. El codigo comun se
// trae con import() dinamico, que si funciona aqui.

let core = null, tareas = null;

self.onmessage = async (e) => {
  const m = e.data;
  if (m.tipo === "init") {
    try {
      core = await import("./detector-core.js");
      tareas = await core.cargar();
      self.postMessage({ tipo: "listo", ok: true });
    } catch (err) {
      self.postMessage({ tipo: "listo", ok: false, error: String(err) });
    }
  } else if (m.tipo === "cuadro") {
    let res = null;
    try {
      res = core.analizar(tareas, m.bitmap, m.t, m.manos);
    } catch { /* un cuadro fallido no detiene nada */ }
    m.bitmap.close();
    self.postMessage({ tipo: "res", id: m.id, ...res });
  }
};

// Deteccion de cara y manos con MediaPipe, compartida por el Worker
// (detector.js) y por la pagina cuando el navegador no permite el Worker.
// Solo guia al usuario: el servidor vuelve a revisar todo en el video.

export const MP = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.35";
const MODELS = "https://storage.googleapis.com/mediapipe-models";

async function createTask(Task, fileset, model, opts) {
  for (const delegate of ["GPU", "CPU"]) {
    try {
      return await Task.createFromOptions(fileset, {
        baseOptions: { modelAssetPath: model, delegate }, runningMode: "VIDEO", ...opts,
      });
    } catch (e) { if (delegate === "CPU") throw e; }
  }
}

export async function cargar() {
  const v = await import(`${MP}/vision_bundle.mjs`);
  const fileset = await v.FilesetResolver.forVisionTasks(`${MP}/wasm`);
  const cara = await createTask(v.FaceLandmarker, fileset,
    `${MODELS}/face_landmarker/face_landmarker/float16/1/face_landmarker.task`,
    { numFaces: 1, outputFaceBlendshapes: true });
  const manos = await createTask(v.HandLandmarker, fileset,
    `${MODELS}/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task`, { numHands: 2 });
  return { cara, manos };
}

// Mismos puntos que app/services/face_track.py. Los cuadros llegan sin espejo
// (el espejo es solo CSS): la izquierda del usuario queda a la derecha de la imagen.
function faceInfo(res) {
  if (!res.faceLandmarks?.length) return null;
  const p = res.faceLandmarks[0];
  const a = p[234].x, b = p[454].x, w = Math.abs(b - a);
  const bs = {};
  (res.faceBlendshapes?.[0]?.categories || []).forEach((c) => { bs[c.categoryName] = c.score; });
  return {
    turn: w > 1e-3 ? (p[1].x - (a + b) / 2) / w : null,
    blink: ((bs.eyeBlinkLeft ?? 0) + (bs.eyeBlinkRight ?? 0)) / 2,
    noseX: p[1].x, chinY: p[152].y,
  };
}

// Devuelve solo lo que usa la pagina: datos de la cara y el centro de cada palma (punto 9).
export function analizar(tareas, imagen, t, conManos) {
  const f = faceInfo(tareas.cara.detectForVideo(imagen, t));
  const hands = conManos ? tareas.manos.detectForVideo(imagen, t).landmarks.map((h) => ({ x: h[9].x, y: h[9].y })) : null;
  return { f, hands };
}

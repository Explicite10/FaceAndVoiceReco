"""Arma la respuesta publica de /api/verify-identity a partir de los analisis internos.

Convenciones del contrato:
- *_percent: 0 a 100 | *_probability: 0 a 1 | *_score: similitud coseno (-1 a 1)
- *_sec: segundos | *_ms: milisegundos
- Cada validacion tiene status "passed" | "failed" | "not_evaluated"; "not_evaluated"
  significa que un paso previo fallo. Se aprueba solo si nada fallo.
"""
import uuid
from datetime import datetime, timezone

from app.core.config import settings

API_VERSION = "1.0.0"

_FACE_MODELS = {
    "face_detection": "YuNet 2023mar",
    "face_recognition": "SFace 2021dec",
    "anti_spoofing": "MiniFASNet V2 + V1SE (Silent-Face)",
}


def _check(status: str, message: str, code: str | None = None) -> dict:
    return {"status": status, "code": code if status == "failed" else None, "message": message}


def _passed(message: str) -> dict:
    return _check("passed", message)


def _failed(code: str, message: str) -> dict:
    return _check("failed", message, code)


def _skipped(why: str) -> dict:
    return _check("not_evaluated", f"No se evaluó: {why}.")


# --------------------------------------------------------------------- rostro
def _face_checks(face: dict) -> dict:
    keys = ("id_face_detected", "video_face_detected", "face_in_all_segments", "face_match", "single_face")
    if not face["ok"]:
        code = face["error_code"]
        checks = {k: _skipped("falló el análisis del rostro") for k in keys}
        if code == "id_face_not_found":
            checks["id_face_detected"] = _failed(code, face["error"])
        elif code == "video_face_not_found":
            checks["video_face_detected"] = _failed(code, face["error"])
        return checks

    r = face["result"]
    segs = r["segments"]
    n = len(segs)
    missing = [s["index"] for s in segs if not s["face_found"]]
    mismatched = [s for s in segs if s["face_found"] and not s["is_match"]]
    mf = r["multiple_faces"]

    return {
        "id_face_detected": _passed("Se detectó la cara en la identificación."),
        "video_face_detected": _passed("Se detectó la cara en el video."),
        "face_in_all_segments": (
            _failed("face_missing_in_segment", f"La cara no aparece en el tramo {', '.join(map(str, missing))} del video.")
            if missing else _passed(f"La cara aparece en los {n} tramos del video.")
        ),
        "face_match": (
            _failed(
                "face_mismatch",
                "La cara no coincide con la identificación en "
                + ", ".join(f"el tramo {s['index']} ({s['similarity_percent']}%)" for s in mismatched)
                + f"; mínimo {r['threshold_percent']:g}%.",
            )
            if mismatched else
            _passed(f"La cara coincide con la identificación en {n - len(missing)} de {n} tramos.")
            if missing else
            _passed(f"La cara coincide con la identificación en los {n} tramos.")
        ),
        "single_face": (
            _failed(
                "multiple_faces_detected",
                f"Se detectaron {mf['max_faces_in_frame']} caras en el video "
                f"({mf['frames']} cuadros, desde el segundo {mf['first_time_sec']}).",
            )
            if mf["detected"] else _passed("Solo aparece una persona en el video.")
        ),
    }


def _face_details(face: dict) -> dict | None:
    if not face["ok"]:
        return None
    r = face["result"]
    mf = r["multiple_faces"]
    return {
        "similarity": {
            "average_percent": r["similarity_percent"],
            "min_score": r["min_similarity"],
            "threshold_score": r["tolerance"],
            "threshold_percent": r["threshold_percent"],
        },
        "segments": [
            {
                "index": s["index"],
                "time_sec": s["time_sec"],
                "face_detected": s["face_found"],
                "similarity_score": s["raw_similarity"],
                "similarity_percent": s["similarity_percent"] if s["face_found"] else None,
                "match": s["is_match"],
            }
            for s in r["segments"]
        ],
        "faces_in_video": {
            "max_per_frame": mf["max_faces_in_frame"],
            "frames_with_multiple": mf["frames"],
            "first_multiple_sec": mf["first_time_sec"],
            "min_frames_to_reject": settings.multi_face_min_frames,
        },
    }


# ----------------------------------------------------------------------- voz
def _voice_checks(voice: dict) -> dict:
    keys = ("audio_present", "audio_duration", "speech_detected", "transcription_confidence", "phrase_match")
    if not voice["ok"]:
        checks = {k: _skipped("falló el análisis de la voz") for k in keys}
        if voice["error_code"] == "audio_missing":
            checks["audio_present"] = _failed("audio_missing", "El video no tiene audio utilizable.")
            for k in keys[1:]:
                checks[k] = _skipped("el video no tiene audio")
        return checks

    r = voice["result"]
    decision = r["decision"]
    a = r["audio"]
    lo, hi = settings.audio_min_duration_sec, settings.audio_max_duration_sec
    checks = {"audio_present": _passed("El video tiene audio.")}

    if decision == "invalid_audio":
        checks["audio_duration"] = _failed(
            "audio_duration_invalid", f"El audio dura {a['duration_sec']} s; debe durar entre {lo:g} y {hi:g} s."
        )
        for k in keys[2:]:
            checks[k] = _skipped("la duración del audio no es válida")
        return checks
    checks["audio_duration"] = _passed(f"El audio dura {a['duration_sec']} s (válido: {lo:g} a {hi:g} s).")

    if decision == "no_speech":
        checks["speech_detected"] = _failed(
            "no_speech_detected",
            f"No hubo voz suficiente ({a['speech_duration_sec']} s; mínimo {settings.min_speech_duration_sec:g} s).",
        )
        for k in keys[3:]:
            checks[k] = _skipped("no se detectó voz")
        return checks
    checks["speech_detected"] = _passed(f"Se detectaron {a['speech_duration_sec']} s de voz.")

    checks["transcription_confidence"] = (
        _failed("speech_low_confidence", "La transcripción no es confiable (ruido o voz poco clara).")
        if decision == "low_confidence" else _passed("La transcripción es confiable.")
    )
    checks["phrase_match"] = (
        _failed("phrase_mismatch", f"La frase no coincide ({r['match_percent']}%; mínimo {r['threshold']:g}%).")
        if r["match_percent"] < r["threshold"] else _passed(f"La frase coincide ({r['match_percent']}%).")
    )
    return checks


def _voice_details(voice: dict) -> dict | None:
    if not voice["ok"]:
        return None
    r = voice["result"]
    asr_ran = r["asr"]["language"] is not None
    return {
        "expected_phrase": r["expected_phrase"],
        "transcript": r["transcript"],
        "phrase_match": {
            "match_percent": r["match_percent"],
            "threshold_percent": r["threshold"],
            "word_error_rate": r["scores"]["wer"] if asr_ran else None,
            "missing_words": r["missing_words"] if asr_ran else [],
            "extra_words": r["extra_words"],
        },
        "audio": {
            "duration_sec": r["audio"]["duration_sec"],
            "speech_sec": r["audio"]["speech_duration_sec"],
            "speech_ratio": r["audio"]["speech_ratio"],
        },
        "transcription": {
            "language": r["asr"]["language"],
            "language_probability": r["asr"]["language_probability"],
            "word_confidence_probability": r["asr"]["mean_word_confidence"],
            "no_speech_probability": r["asr"]["no_speech_prob"],
            "avg_log_probability": r["asr"]["avg_logprob"],
        } if asr_ran else None,
    }


# ------------------------------------------------------------ prueba de vida
def _liveness_checks(liveness: dict) -> dict:
    keys = ("anti_spoofing", "face_tracking", "lip_movement", "lip_sync")
    if not liveness["ok"]:
        return {k: _skipped("no hay rostro en el video") for k in keys}

    r = liveness["result"]
    a = r["anti_spoof"]
    lip = r["lip_sync"]
    code = lip["reason_code"]
    real_pct = round(a["real_score"] * 100)
    checks = {
        "anti_spoofing": (
            _passed(f"La cara parece real, no una pantalla ni una foto ({real_pct}%).")
            if a["passed"] else
            _failed(
                "spoof_detected",
                f"Posible pantalla, foto o máscara (probabilidad de cara real {real_pct}%; "
                f"mínimo {round(a['threshold'] * 100)}%).",
            )
        )
    }

    if code in ("no_audio", "unreadable_video"):
        for k in keys[1:]:
            checks[k] = _skipped(lip["reason"])
        return checks

    tracked_pct = round(lip["tracked_ratio"] * 100)
    if code == "face_not_tracked":
        checks["face_tracking"] = _failed("face_not_tracked", f"La cara solo se ve en el {tracked_pct}% del video.")
        checks["lip_movement"] = _skipped("no se ve la cara la mayor parte del video")
        checks["lip_sync"] = _skipped("no se ve la cara la mayor parte del video")
        return checks
    checks["face_tracking"] = _passed(f"La cara se ve en el {tracked_pct}% del video.")

    if code == "lips_not_moving":
        checks["lip_movement"] = _failed("lips_not_moving", "La boca no se mueve al hablar.")
        checks["lip_sync"] = _skipped("la boca no se mueve")
        return checks
    checks["lip_movement"] = _passed("La boca se mueve al hablar.")
    checks["lip_sync"] = (
        _failed("lips_out_of_sync", "El movimiento de la boca no coincide con la voz.")
        if code == "lips_out_of_sync" else _passed("El movimiento de la boca coincide con la voz.")
    )
    return checks


def _liveness_details(liveness: dict) -> dict | None:
    if not liveness["ok"]:
        return None
    a = liveness["result"]["anti_spoof"]
    lip = liveness["result"]["lip_sync"]
    return {
        "anti_spoofing": {
            "real_probability": a["real_score"],
            "threshold_probability": a["threshold"],
            "by_segment_probability": a["segment_scores"],
        },
        "lip_sync": {
            "face_tracked_ratio": lip["tracked_ratio"],
            "mouth_motion_speaking": lip["mouth_motion_speech"],
            "mouth_motion_silent": lip["mouth_motion_silence"],
            "audio_correlation": lip["correlation"],
            "audio_lag_ms": lip["lag_ms"],
        },
    }


# --------------------------------------------------------------------- reto
_GESTURE_CHECK = {
    "turn_left": ("head_turn", "girar la cabeza a la izquierda"),
    "turn_right": ("head_turn", "girar la cabeza a la derecha"),
    "blink": ("blink", "parpadear"),
    "raise_left_hand": ("hand_raise", "alzar la mano izquierda"),
    "raise_right_hand": ("hand_raise", "alzar la mano derecha"),
}


def _challenge_checks(challenge: dict | None, gestures: dict | None) -> dict:
    if challenge is None:
        return {"challenge_used": _skipped("la solicitud no usó un reto (challenge_id)")}

    names = [_GESTURE_CHECK[g][0] for g in challenge["gestures"]] + ["gesture_order"]
    if not gestures["ok"]:
        return {k: _skipped(gestures["error"].rstrip(".").lower()) for k in names}

    r = gestures["result"]
    checks = {}
    for step in r["steps"]:
        key, label = _GESTURE_CHECK[step["gesture"]]
        checks[key] = (
            _passed(f"Se detectó el gesto: {label} (segundo {step['time_sec']}).")
            if step["detected"] else
            _failed(f"{key}_not_detected", f"No se detectó el gesto: {label}.")
        )
    if not all(s["detected"] for s in r["steps"]):
        checks["gesture_order"] = _skipped("faltó al menos un gesto")
    else:
        checks["gesture_order"] = (
            _passed("Los gestos se hicieron en el orden pedido.")
            if r["in_order"] else
            _failed("gesture_order_mismatch", "Los gestos no se hicieron en el orden pedido.")
        )
    return checks


def _challenge_details(challenge: dict | None, gestures: dict | None) -> dict | None:
    if challenge is None:
        return None
    r = gestures["result"] if gestures["ok"] else None
    return {
        "challenge_id": challenge["challenge_id"],
        "phrase": challenge["phrase"],
        "gestures": r["steps"] if r else [
            {"gesture": g, "detected": False, "time_sec": None} for g in challenge["gestures"]
        ],
        "in_order": r["in_order"] if r else False,
        "measurements": {
            "frames_analyzed": r["frames"],
            "max_turn_left": r["max_turn_left"],
            "max_turn_right": r["max_turn_right"],
            "max_blink_probability": r["max_blink"],
            "turn_threshold": settings.gesture_turn_offset,
            "blink_threshold_probability": settings.gesture_blink_closed,
        } if r else None,
    }


# ---------------------------------------------------------------- respuesta
def build_report(
    *, face: dict, voice: dict, liveness: dict, challenge: dict | None, gestures: dict | None, elapsed_ms: int
) -> dict:
    checks = {
        "face": _face_checks(face),
        "voice": _voice_checks(voice),
        "liveness": _liveness_checks(liveness),
        "challenge": _challenge_checks(challenge, gestures),
    }
    failed = [
        (f"{group}.{name}", c) for group, items in checks.items() for name, c in items.items() if c["status"] == "failed"
    ]
    parts = (("face", face), ("voice", voice), ("liveness", liveness), ("challenge", gestures))
    errors = [
        {"component": name, "code": part["error_code"], "message": part["error"], "http_status": part["status_code"]}
        for name, part in parts
        if part is not None and not part["ok"]
    ]
    approved = not failed and not errors

    if approved:
        summary = "Aprobado: el rostro coincide con la identificación, la frase es correcta y pasó la prueba de vida."
        if challenge is not None:
            summary = summary[:-1] + " con los gestos pedidos."
    else:
        problems = [c["message"].rstrip(".") for _, c in failed]
        failed_codes = {c["code"] for _, c in failed}
        problems += [e["message"].rstrip(".") for e in errors if e["code"] not in failed_codes and e["component"] != "liveness"]
        summary = "Rechazado: " + "; ".join(x[:1].lower() + x[1:] for x in problems) + "."

    return {
        "request_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "processing_time_ms": elapsed_ms,
        "result": {
            "decision": "approved" if approved else "rejected",
            "approved": approved,
            "failed_checks": [name for name, _ in failed],
            "summary": summary,
        },
        "checks": checks,
        "details": {
            "face": _face_details(face),
            "voice": _voice_details(voice),
            "liveness": _liveness_details(liveness),
            "challenge": _challenge_details(challenge, gestures),
        },
        "errors": errors,
        "meta": {
            "api_version": API_VERSION,
            "models": {
                "speech_to_text": f"faster-whisper {settings.asr_model_size} ({settings.asr_device}/{settings.asr_compute_type})",
                **_FACE_MODELS,
                "face_landmarks": "MediaPipe Face Landmarker v1",
                "hand_landmarks": "MediaPipe Hand Landmarker v1",
            },
        },
    }


# ------------------------------------------- resumen para proyectos (sesiones)
# Por que fallo, en palabras del usuario y con como corregirlo. Una entrada por
# codigo de check fallido; el orden sigue al de los grupos del reporte.
_MOTIVOS = {
    "video_face_not_found": "No se vio tu rostro en el video. Acércate a la cámara y busca buena luz.",
    "face_missing_in_segment": "Tu rostro no se vio durante todo el video. Mantente frente a la cámara.",
    "face_mismatch": "Tu rostro no coincide con la foto de la identificación.",
    "multiple_faces_detected": "Se detectó más de una persona. Asegúrate de estar solo frente a la cámara.",
    "audio_missing": "No se grabó el audio. Revisa el permiso del micrófono.",
    "audio_duration_invalid": "La grabación fue muy corta o muy larga.",
    "no_speech_detected": "No se escuchó tu voz. Habla más fuerte y cerca del micrófono.",
    "speech_low_confidence": "No se entendió la voz. Busca un lugar sin ruido.",
    "phrase_mismatch": "La frase no coincide con la que se pidió. Léela completa, en voz alta y clara.",
    "spoof_detected": "Parece una foto o una pantalla. Debes estar tú frente a la cámara.",
    "face_not_tracked": "Tu rostro salió de la imagen gran parte del tiempo. Mantente frente a la cámara.",
    "lips_not_moving": "No se vio que movieras los labios al hablar. Lee la frase mirando a la cámara.",
    "lips_out_of_sync": "El movimiento de tus labios no coincidió con tu voz.",
    "head_turn_not_detected": "No se detectó el giro de cabeza. Gira un poco más cuando se te pida.",
    "blink_not_detected": "No se detectó el parpadeo. Cierra bien los ojos un momento.",
    "hand_raise_not_detected": "No se detectó la mano alzada. Muéstrala a un lado de tu cara.",
    "gesture_order_mismatch": "Los movimientos no se hicieron en el orden indicado.",
}
MOTIVO_GENERICO = "No pudimos verificar tu identidad. Intenta de nuevo con buena luz, de frente a la cámara."


def user_reasons(report: dict | None) -> list[str]:
    """Motivos del rechazo para mostrar al usuario. Vacio si se aprobo."""
    if report is None:
        return ["Ocurrió un error al analizar el video. Intenta de nuevo."]
    if report["result"]["approved"]:
        return []
    motivos: list[str] = []
    for grupo in ("face", "voice", "liveness", "challenge"):
        for check in report["checks"].get(grupo, {}).values():
            texto = _MOTIVOS.get(check["code"]) if check["status"] == "failed" else None
            if texto and texto not in motivos:
                motivos.append(texto)
    return motivos or [MOTIVO_GENERICO]


def _flag(check: dict | None) -> bool | None:
    """passed -> True, failed -> False, not_evaluated o ausente -> None."""
    if not check or check["status"] == "not_evaluated":
        return None
    return check["status"] == "passed"


def _pct(x: float | None) -> float | None:
    return None if x is None else round(x * 100, 1)


def build_summary(report: dict) -> dict:
    """Banderas y porcentajes del reporte, en un formato corto para los proyectos.

    Cada bandera es true (paso), false (fallo) o null (no se pudo evaluar
    porque fallo un paso previo). El detalle completo sigue en el reporte.
    """
    details = report["details"]
    checks = {g: report["checks"].get(g, {}) for g in ("face", "voice", "liveness", "challenge")}
    face, voice, live, chal = details.get("face"), details.get("voice"), details.get("liveness"), details.get("challenge")
    segments = [s["similarity_percent"] for s in (face["segments"] if face else []) if s["face_detected"]]
    return {
        "aprobada": report["result"]["approved"],
        "checks_fallidos": report["result"]["failed_checks"],
        "motivos": user_reasons(report),
        "rostro": {
            "coincide": _flag(checks["face"].get("face_match")),
            "similitud_porcentaje": face["similarity"]["average_percent"] if face else None,
            "similitud_minima_porcentaje": min(segments) if segments else None,
            "umbral_porcentaje": face["similarity"]["threshold_percent"] if face else None,
            "una_sola_persona": _flag(checks["face"].get("single_face")),
        },
        "frase": {
            "coincide": _flag(checks["voice"].get("phrase_match")),
            "coincidencia_porcentaje": voice["phrase_match"]["match_percent"] if voice else None,
            "umbral_porcentaje": voice["phrase_match"]["threshold_percent"] if voice else None,
            "esperada": voice["expected_phrase"] if voice else None,
            "transcripcion": voice["transcript"] if voice else None,
        },
        "vida": {
            "persona_real": _flag(checks["liveness"].get("anti_spoofing")),
            "probabilidad_real_porcentaje": _pct(live["anti_spoofing"]["real_probability"]) if live else None,
            "umbral_porcentaje": _pct(live["anti_spoofing"]["threshold_probability"]) if live else None,
            "labios_sincronizados": _flag(checks["liveness"].get("lip_sync")),
        },
        "gestos": {
            "completos": all(g["detected"] for g in chal["gestures"]) if chal else None,
            "en_orden": _flag(checks["challenge"].get("gesture_order")),
            "detalle": [{"gesto": g["gesture"], "detectado": g["detected"], "segundo": g["time_sec"]}
                        for g in chal["gestures"]] if chal else [],
        },
    }


# ------------------------------------------------- foto contra identificacion
def _photo_checks(a: dict) -> dict:
    id_face, photo_face = a["id_face"], a["photo_face"]
    checks = {
        "face": {
            "id_face_detected": (
                _passed("Se detectó la cara en la identificación.") if id_face else
                _failed("id_face_not_found", "No se detectó rostro en la identificación.")
            ),
            "photo_face_detected": (
                _passed("Se detectó la cara en la foto.") if photo_face else
                _failed("photo_face_not_found", "No se detectó rostro en la foto.")
            ),
        },
    }
    if photo_face is None:
        why = "no se detectó rostro en la foto"
        checks["face"]["single_face"] = _skipped(why)
        checks["face"]["face_match"] = _skipped(why)
        checks["quality"] = {k: _skipped(why) for k in ("face_size", "sharpness", "brightness")}
        checks["liveness"] = {"anti_spoofing": _skipped(why)}
        return checks

    n = a["faces_in_photo"]
    checks["face"]["single_face"] = (
        _passed("Solo aparece una persona en la foto.") if n == 1 else
        _failed("multiple_faces_detected", f"Se detectaron {n} caras en la foto.")
    )
    pct = a["similarity_percent"]
    if id_face is None:
        checks["face"]["face_match"] = _skipped("no se detectó rostro en la identificación")
    elif a["is_match"]:
        checks["face"]["face_match"] = _passed(f"La cara coincide con la identificación ({pct}%).")
    else:
        checks["face"]["face_match"] = _failed(
            "face_mismatch",
            f"La cara no coincide con la identificación ({pct}%; mínimo {a['threshold_percent']:g}%).",
        )

    q = photo_face["quality"]
    lo, hi = settings.photo_min_brightness, settings.photo_max_brightness
    if q["brightness"] < lo:
        brightness = _failed("image_too_dark", f"La foto está muy oscura (brillo {q['brightness']:g}; mínimo {lo:g}).")
    elif q["brightness"] > hi:
        brightness = _failed("image_overexposed", f"La foto está sobreexpuesta (brillo {q['brightness']:g}; máximo {hi:g}).")
    else:
        brightness = _passed(f"La iluminación es adecuada (brillo {q['brightness']:g}).")
    checks["quality"] = {
        "face_size": (
            _passed(f"La cara tiene buen tamaño ({q['size_px']} px).")
            if q["size_px"] >= settings.photo_min_face_px else
            _failed("face_too_small", f"La cara mide {q['size_px']} px; mínimo {settings.photo_min_face_px} px.")
        ),
        "sharpness": (
            _passed(f"La foto es nítida (nitidez {q['sharpness']:g}).")
            if q["sharpness"] >= settings.photo_min_sharpness else
            _failed("image_blurry", f"La foto está borrosa (nitidez {q['sharpness']:g}; mínimo {settings.photo_min_sharpness:g}).")
        ),
        "brightness": brightness,
    }

    real_pct = round(photo_face["real_probability"] * 100)
    checks["liveness"] = {
        "anti_spoofing": (
            _passed(f"La cara parece real, no una pantalla ni una foto ({real_pct}%).")
            if photo_face["real_probability"] >= settings.antispoof_threshold else
            _failed(
                "spoof_detected",
                f"Posible pantalla, foto o máscara (probabilidad de cara real {real_pct}%; "
                f"mínimo {round(settings.antispoof_threshold * 100)}%).",
            )
        ),
    }
    return checks


def build_photo_report(analysis: dict, *, elapsed_ms: int) -> dict:
    a = analysis
    checks = _photo_checks(a)
    failed = [
        (f"{group}.{name}", c) for group, items in checks.items() for name, c in items.items() if c["status"] == "failed"
    ]
    approved = not failed
    if approved:
        summary = "Aprobado: la foto coincide con la identificación, tiene buena calidad y la cara parece real."
    else:
        problems = [c["message"].rstrip(".") for _, c in failed]
        summary = "Rechazado: " + "; ".join(x[:1].lower() + x[1:] for x in problems) + "."

    id_face, photo_face = a["id_face"], a["photo_face"]
    return {
        "request_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "processing_time_ms": elapsed_ms,
        "result": {
            "decision": "approved" if approved else "rejected",
            "approved": approved,
            "failed_checks": [name for name, _ in failed],
            "summary": summary,
        },
        "checks": checks,
        "details": {
            "similarity": None if a["similarity"] is None else {
                "score": a["similarity"],
                "percent": a["similarity_percent"],
                "threshold_score": a["threshold_score"],
                "threshold_percent": a["threshold_percent"],
            },
            "faces_in_photo": a["faces_in_photo"],
            "id_face_quality": id_face["quality"] if id_face else None,
            "photo_face_quality": photo_face["quality"] if photo_face else None,
            "anti_spoofing": None if photo_face is None else {
                "real_probability": photo_face["real_probability"],
                "threshold_probability": settings.antispoof_threshold,
            },
        },
        "meta": {"api_version": API_VERSION, "models": dict(_FACE_MODELS)},
    }

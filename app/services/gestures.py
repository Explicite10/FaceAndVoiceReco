"""Gestos del reto: girar la cabeza, parpadear y alzar una mano, en el orden pedido.

El navegador ya los detecta para guiar al usuario, pero eso no se confia: aqui
se vuelven a buscar en el video subido con las series de face_track.py.
Izquierda/derecha son las del usuario. El cuadro viene sin espejo, asi que su
izquierda queda a la DERECHA de la imagen (x mayor).
"""
import numpy as np

from app.core.config import settings


def _runs(mask: np.ndarray, min_len: int) -> list[int]:
    """Indices donde empieza cada racha de al menos min_len valores True."""
    starts, run = [], 0
    for i, v in enumerate(mask):
        run = run + 1 if v else 0
        if run == min_len:
            starts.append(i - min_len + 1)
    return starts


def _turn_events(track: dict, side: str) -> list[float]:
    turn = track["turn"]
    with np.errstate(invalid="ignore"):
        mask = turn > settings.gesture_turn_offset if side == "left" else turn < -settings.gesture_turn_offset
    return [float(track["times"][i]) for i in _runs(mask, settings.gesture_turn_min_frames)]


def _blink_events(track: dict) -> list[float]:
    """Cada cierre de ojos con los ojos abiertos antes y despues."""
    events, seen_open, closed_at = [], False, None
    for t, b in zip(track["times"], track["blink"]):
        if np.isnan(b):
            continue
        if b <= settings.gesture_blink_open:
            if closed_at is not None:
                events.append(float(closed_at))
                closed_at = None
            seen_open = True
        elif b >= settings.gesture_blink_closed and seen_open and closed_at is None:
            closed_at = t
    return events


def _hand_events(track: dict, side: str) -> list[float]:
    """Mano a la altura de la cara o mas arriba, del lado pedido de la nariz."""
    times, hands_by_frame = track["times"], track["hands"]
    nose_x, chin_y = track["nose_x"], track["chin_y"]
    sampled = [i for i, h in enumerate(hands_by_frame) if h is not None]
    mask = np.zeros(len(sampled), dtype=bool)
    last_nose = last_chin = np.nan
    for j, i in enumerate(sampled):
        # La mano puede tapar la cara: se usa la ultima posicion conocida.
        if not np.isnan(nose_x[i]):
            last_nose, last_chin = nose_x[i], chin_y[i]
        if np.isnan(last_nose):
            continue
        for x, y in hands_by_frame[i]:
            on_side = x > last_nose if side == "left" else x < last_nose
            if on_side and y < last_chin:
                mask[j] = True
                break
    return [float(times[sampled[j]]) for j in _runs(mask, settings.gesture_hand_min_frames)]


def _events(track: dict, gesture: str) -> list[float]:
    if gesture in ("turn_left", "turn_right"):
        return _turn_events(track, gesture.removeprefix("turn_"))
    if gesture == "blink":
        return _blink_events(track)
    if gesture in ("raise_left_hand", "raise_right_hand"):
        return _hand_events(track, gesture.split("_")[1])
    raise ValueError(f"Gesto desconocido: {gesture}")


def analyze_gestures(track: dict, gestures: list[str]) -> dict:
    """Busca cada gesto y revisa que aparezcan en el orden pedido.

    El orden se valida encadenado: el gesto 2 debe ocurrir despues del primer
    gesto 1, el 3 despues de ese gesto 2, etc.
    """
    found = {g: _events(track, g) for g in gestures}
    steps, prev, in_order = [], -1.0, True
    for g in gestures:
        nxt = next((t for t in found[g] if t > prev), None)
        if nxt is None:
            in_order = False
        else:
            prev = nxt
        steps.append({
            "gesture": g,
            "detected": bool(found[g]),
            "time_sec": round(found[g][0], 2) if found[g] else None,
        })

    turn, blink = track["turn"], track["blink"]
    has_turn, has_blink = np.any(~np.isnan(turn)), np.any(~np.isnan(blink))
    in_order = in_order and all(s["detected"] for s in steps)
    return {
        "steps": steps,
        "in_order": in_order,
        # Momento en que termino el ultimo gesto de la cadena: la frase va despues.
        "last_gesture_sec": round(prev, 2) if in_order else None,
        "frames": len(track["times"]),
        # Valores maximos medidos, para calibrar los umbrales.
        "max_turn_left": round(max(0.0, float(np.nanmax(turn))), 3) if has_turn else None,
        "max_turn_right": round(max(0.0, float(-np.nanmin(turn))), 3) if has_turn else None,
        "max_blink": round(float(np.nanmax(blink)), 3) if has_blink else None,
    }

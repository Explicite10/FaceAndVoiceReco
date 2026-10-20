"""Decision de movimiento de labios con valores medidos en intentos reales."""
from app.services.lip_sync import lip_decision


def test_persona_real_que_fallaba_con_el_umbral_anterior():
    # Intento real: boca 0.00317 al hablar, 0.00013 en silencio, correlacion 0.71.
    assert lip_decision(1.0, 0.00317, 0.00013, 0.711) is None


def test_persona_real_aprobada_antes_sigue_pasando():
    assert lip_decision(1.0, 0.00532, 0.00021, 0.736) is None


def test_imagen_quieta_con_audio():
    # Foto o pantalla: solo el temblor del detector, igual hablando que callado.
    assert lip_decision(1.0, 0.0003, 0.00025, 0.05) == "lips_not_moving"


def test_boca_que_se_mueve_igual_hablando_que_callada():
    assert lip_decision(1.0, 0.003, 0.0015, 0.5) == "lips_not_moving"


def test_audio_doblado():
    # La boca se mueve, pero no al ritmo de la voz.
    assert lip_decision(1.0, 0.004, 0.0002, 0.05) == "lips_out_of_sync"


def test_cara_fuera_de_cuadro():
    assert lip_decision(0.3, 0.004, 0.0002, 0.7) == "face_not_tracked"


def _serie(rng, gestos_ruidosos=True, habla=True):
    """20 s a 15 fps: gestos 1-7 s (giro y mano), frase hablada 9-17 s."""
    import numpy as np
    t = np.arange(0, 20, 1 / 15)
    ap = 0.02 + rng.normal(0, 0.0001, len(t))           # boca quieta: solo temblor del detector
    turn = np.zeros(len(t))
    hands = [[] for _ in t]
    giro = (t > 1) & (t < 3)
    mano = (t > 4) & (t < 6)
    if gestos_ruidosos:
        turn[giro] = 0.25
        ap[giro | mano] += rng.normal(0, 0.006, (giro | mano).sum())   # la medida salta con el gesto
        for i in np.flatnonzero(mano):
            hands[i] = [(0.3, 0.4)]
    frase = (t > 9) & (t < 17)
    if habla:
        ap[frase] += 0.01 * np.abs(np.sin(t[frase] * 9))   # boca abriendo y cerrando al leer
    speech = frase.copy()
    return t, ap, turn, hands, speech


def test_gestos_ya_no_inflan_el_silencio():
    import numpy as np
    from app.services.lip_sync import _mean_motion, medir_movimiento
    t, ap, turn, hands, speech = _serie(np.random.default_rng(1))
    # Como se media antes: el silencio incluia los gestos y la proporcion no llegaba a 3x.
    assert lip_decision(1.0, _mean_motion(ap, speech), _mean_motion(ap, ~speech), 0.6) == "lips_not_moving"
    habla, silencio = medir_movimiento(t, ap, turn, hands, speech, (9.0, 17.0))
    assert lip_decision(1.0, habla, silencio, 0.6) is None


def test_foto_sigue_fallando_aunque_se_excluyan_los_gestos():
    import numpy as np
    from app.services.lip_sync import medir_movimiento
    t, ap, turn, hands, speech = _serie(np.random.default_rng(2), habla=False)
    habla, silencio = medir_movimiento(t, ap, turn, hands, speech, (9.0, 17.0))
    assert lip_decision(1.0, habla, silencio, 0.05) == "lips_not_moving"

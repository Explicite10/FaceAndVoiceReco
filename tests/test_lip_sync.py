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

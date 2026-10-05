import re
import unicodedata

_UNITS = [
    "cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho",
    "nueve", "diez", "once", "doce", "trece", "catorce", "quince", "dieciseis",
    "diecisiete", "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidos",
    "veintitres", "veinticuatro", "veinticinco", "veintiseis", "veintisiete",
    "veintiocho", "veintinueve",
]
_TENS = {
    30: "treinta", 40: "cuarenta", 50: "cincuenta", 60: "sesenta",
    70: "setenta", 80: "ochenta", 90: "noventa",
}
_HUNDREDS = {
    100: "cien", 200: "doscientos", 300: "trescientos", 400: "cuatrocientos",
    500: "quinientos", 600: "seiscientos", 700: "setecientos",
    800: "ochocientos", 900: "novecientos",
}


def _int_to_words_es(n: int) -> str:
    if n < 0 or n > 999:
        return str(n)
    if n < 30:
        return _UNITS[n]
    if n < 100:
        tens, rest = (n // 10) * 10, n % 10
        return _TENS[tens] if rest == 0 else f"{_TENS[tens]} y {_UNITS[rest]}"
    hundreds, rest = (n // 100) * 100, n % 100
    if rest == 0:
        return _HUNDREDS[hundreds]
    head = "ciento" if hundreds == 100 else _HUNDREDS[hundreds]
    return f"{head} {_int_to_words_es(rest)}"


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def normalize_phrase(text: str) -> str:
    """Deja el texto comparable: sin acentos, sin puntuacion, numeros en palabras.

    Whisper transcribe numeros hablados como digitos ("20" en vez de "veinte"),
    asi que ambos lados se convierten a palabras antes de comparar.
    """
    if not text:
        return ""

    text = _strip_accents(text.lower())
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\d+", lambda m: _int_to_words_es(int(m.group())), text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

import jiwer
from rapidfuzz import fuzz

from app.core.config import settings
from app.services.text_norm import normalize_phrase


def _collect_diff(out: jiwer.WordOutput) -> tuple[list[str], list[str]]:
    reference = out.references[0]
    hypothesis = out.hypotheses[0]
    missing: list[str] = []
    extra: list[str] = []

    for chunk in out.alignments[0]:
        if chunk.type in ("delete", "substitute"):
            missing.extend(reference[chunk.ref_start_idx : chunk.ref_end_idx])
        if chunk.type in ("insert", "substitute"):
            extra.extend(hypothesis[chunk.hyp_start_idx : chunk.hyp_end_idx])

    return missing, extra


def _score(expected_norm: str, heard_norm: str) -> tuple[float, jiwer.WordOutput, float, float]:
    out = jiwer.process_words(expected_norm, heard_norm)
    wer_score = max(0.0, 1.0 - out.wer) * 100.0
    fuzzy_ratio = fuzz.ratio(expected_norm, heard_norm)
    weight = settings.wer_weight
    return weight * wer_score + (1.0 - weight) * fuzzy_ratio, out, wer_score, fuzzy_ratio


def _best_window(expected_norm: str, heard_words: list[str]) -> tuple[int, int]:
    """El tramo seguido de lo escuchado que mas se parece a la frase.

    Solo recorta palabras de mas al principio y al final ("ya empezo?",
    "listo"): lo que hay en medio de la frase se califica completo.
    """
    n = len(expected_norm.split())
    total = len(heard_words)
    if total <= n:
        return 0, total
    best, best_score = (0, total), _score(expected_norm, " ".join(heard_words))[0]
    holgura = max(2, n // 4)
    for i in range(total):
        for j in range(max(i + 1, i + n - holgura), min(total, i + n + holgura) + 1):
            score = _score(expected_norm, " ".join(heard_words[i:j]))[0]
            if score > best_score + 1e-9:
                best, best_score = (i, j), score
    return best


def score_phrase(expected: str, heard: str) -> dict:
    """Compara lo transcrito contra la frase esperada.

    Combina WER (nivel palabra, sensible al orden) con ratio de Levenshtein
    (nivel caracter). Se evita token_set_ratio a proposito: ignora el orden,
    asi que daria 100% a alguien que dijera las palabras revueltas. Las
    palabras de mas antes o despues de la frase no cuentan (ignored_words).
    """
    expected_norm = normalize_phrase(expected)
    heard_norm = normalize_phrase(heard)

    if not heard_norm:
        return {
            "match_percent": 0.0,
            "wer": 1.0,
            "wer_score": 0.0,
            "fuzzy_ratio": 0.0,
            "expected_normalized": expected_norm,
            "heard_normalized": "",
            "missing_words": expected_norm.split(),
            "extra_words": [],
            "ignored_words": [],
        }

    words = heard_norm.split()
    i, j = _best_window(expected_norm, words)
    ignored = words[:i] + words[j:]
    heard_norm = " ".join(words[i:j])
    match_percent, out, wer_score, fuzzy_ratio = _score(expected_norm, heard_norm)
    missing, extra = _collect_diff(out)

    return {
        "match_percent": round(match_percent, 2),
        "wer": round(out.wer, 4),
        "wer_score": round(wer_score, 2),
        "fuzzy_ratio": round(fuzzy_ratio, 2),
        "expected_normalized": expected_norm,
        "heard_normalized": heard_norm,
        "missing_words": missing,
        "extra_words": extra,
        "ignored_words": ignored,
    }

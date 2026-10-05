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


def score_phrase(expected: str, heard: str) -> dict:
    """Compara lo transcrito contra la frase esperada.

    Combina WER (nivel palabra, sensible al orden) con ratio de Levenshtein
    (nivel caracter). Se evita token_set_ratio a proposito: ignora el orden,
    asi que daria 100% a alguien que dijera las palabras revueltas.
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
        }

    out = jiwer.process_words(expected_norm, heard_norm)
    wer_score = max(0.0, 1.0 - out.wer) * 100.0
    fuzzy_ratio = fuzz.ratio(expected_norm, heard_norm)

    weight = settings.wer_weight
    match_percent = weight * wer_score + (1.0 - weight) * fuzzy_ratio
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
    }

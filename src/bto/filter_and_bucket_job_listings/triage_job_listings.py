"""Build the title and JD sent to triage and validate Gemini's answer.

Check the profession, scope verdict and supporting quote before an answer can
be stored or reused. Evidence must come from the supplied listing."""

import re
import unicodedata


def response_schema(directory=None):
    """Build the common answer shape using the private screening labels."""
    from .config import triage_verdicts

    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["profession", "quote", "verdict"],
        "properties": {
            "profession": {"type": "string"},
            "quote": {"type": "string"},
            "verdict": {"type": "string", "enum": triage_verdicts(directory)},
        },
    }


def normalize_quote(value):
    value = unicodedata.normalize("NFKC", value).translate(
        str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', **{v: "-" for v in "‐‑‒–—−"}})
    )
    value = re.sub(r"^\s*(?:[•·▪*]\s*|-\s+|\d+[.)]\s+)", "", value)
    return " ".join(value.split()).casefold()


def quote_found(quote, row):
    normalized = normalize_quote(quote)
    return bool(normalized) and any(
        normalized in normalize_quote(value)
        for value in [row.get("title") or "", (row.get("description_text") or "")[:16000]]
    )


def listing_input(row, template):
    return template.format(
        title=row.get("title") or "", jd=(row.get("description_text") or "")[:16000]
    )


def validate_triage(answer, row, *, fixed=""):
    """Check the scope verdict and require supporting evidence from the supplied listing."""
    if not isinstance(answer, dict) or set(answer) != {"profession", "quote", "verdict"}:
        raise ValueError("Triage needs exactly profession, quote and verdict")
    if not all(isinstance(answer[k], str) and answer[k].strip() for k in answer):
        raise ValueError("Triage fields must be non-empty strings")
    from .config import triage_verdicts

    if answer["verdict"] not in triage_verdicts():
        raise ValueError("Invalid triage verdict")
    if len(answer["profession"].split()) > 12:
        raise ValueError("Profession exceeds 12 words")
    if not quote_found(answer["quote"], row):
        message = (
            "Quote was not found in the sent title or JD. Quote ONLY exact words from "
            "the supplied title/JD; do not quote or paraphrase rules, examples or instructions."
        )
        if normalize_quote(answer["quote"]) in normalize_quote(fixed):
            message += " The previous quote copied instruction/rule text; instruction/rule text is not listing evidence."
        raise ValueError(message)
    result = dict(answer)
    if len(result["quote"].split()) > 30:
        result["quote"] = " ".join(result["quote"].split()[:30])
    return result

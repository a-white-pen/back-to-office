"""Assemble triage input and validate its checked reusable answer."""

import re
import unicodedata

VERDICTS = (
    "not_english",
    "one_package",
    "chip_or_electronics_design",
    "research_publication",
    "pass",
    "non_tech",
)
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["profession", "quote", "verdict"],
    "properties": {
        "profession": {"type": "string"},
        "quote": {"type": "string"},
        "verdict": {"type": "string", "enum": list(VERDICTS)},
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


def validate_triage(answer, row):
    if not isinstance(answer, dict) or set(answer) != {"profession", "quote", "verdict"}:
        raise ValueError("Triage needs exactly profession, quote and verdict")
    if not all(isinstance(answer[k], str) and answer[k].strip() for k in answer):
        raise ValueError("Triage fields must be non-empty strings")
    if answer["verdict"] not in VERDICTS:
        raise ValueError("Invalid triage verdict")
    if len(answer["profession"].split()) > 12:
        raise ValueError("Profession exceeds 12 words")
    if not quote_found(answer["quote"], row):
        raise ValueError("Quote was not found in the sent title or JD")
    result = dict(answer)
    if len(result["quote"].split()) > 30:
        result["quote"] = " ".join(result["quote"].split()[:30])
    return result

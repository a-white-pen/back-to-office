"""Identify clearly non-English JDs before sending listings to Gemini.

Use fixed checks of letter scripts and common English words, returning a
reason when a JD is excluded."""

import re
import unicodedata
from collections import Counter

COMMON_WORDS = frozenset(
    [
        "the",
        "and",
        "of",
        "to",
        "in",
        "a",
        "for",
        "with",
        "on",
        "is",
        "as",
        "or",
        "you",
        "our",
        "we",
        "be",
        "by",
        "will",
        "are",
        "an",
        "at",
        "this",
        "that",
        "from",
        "your",
        "have",
        "work",
        "experience",
        "team",
        "role",
        "skills",
        "years",
        "ability",
        "business",
        "support",
    ]
)


def filter_language(description):
    """Return an exclusion reason only when the fixed language checks clearly fail."""
    text = description or ""
    letters = [c for c in text if c.isalpha()]
    foreign = [c for c in letters if "LATIN" not in unicodedata.name(c, "")]
    if len(letters) >= 200 and len(foreign) * 5 >= len(letters) * 2:
        scripts = Counter()
        for character in foreign:
            name = unicodedata.name(character, "")
            script = next(
                (
                    display
                    for tokens, display in [
                        (("THAI",), "Thai"),
                        (("HIRAGANA", "KATAKANA"), "Japanese"),
                        (("HANGUL",), "Korean"),
                        (("CJK", "IDEOGRAPH"), "Chinese"),
                        (("ARABIC",), "Arabic"),
                        (("CYRILLIC",), "Cyrillic"),
                    ]
                    if any(t in name for t in tokens)
                ),
                "non-Latin",
            )
            scripts[script] += 1
        script = scripts.most_common(1)[0][0]
        return f"JD not in English: {len(foreign) * 100 // len(letters)}% {script} letters"
    words = re.findall(r"[A-Za-z]+", text)
    if len(words) >= 60 and sum(word.casefold() in COMMON_WORDS for word in words) * 15 < len(
        words
    ):
        return "JD not in English: few common English words"
    return None

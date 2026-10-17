"""Cut plainly excluded title phrases, unless a protecting phrase occurs."""

import re


def phrase_pattern(phrase):
    words = re.split(r"[\s/-]+", phrase.casefold())
    last = words[-1]
    plural = (
        last[:-1] + "ies"
        if last.endswith("y") and len(last) > 1 and last[-2] not in "aeiou"
        else last + "es"
        if last.endswith(("s", "x", "z", "ch", "sh"))
        else last + "s"
    )
    return (
        r"(?<!\w)"
        + r"[\s/-]+".join(
            [*map(re.escape, words[:-1]), "(?:" + re.escape(last) + "|" + re.escape(plural) + ")"]
        )
        + r"(?!\w)"
    )


def filter_title(title, fail_phrases, protect_words):
    text = title or ""
    for protect in protect_words:
        for match in re.finditer(phrase_pattern(protect), text, re.IGNORECASE):
            if protect.casefold() == "data" and re.match(
                r"[\s/-]+entr(?:y|ies)\b", text[match.end() :], re.IGNORECASE
            ):
                continue
            return None
    for phrase in fail_phrases:
        if re.search(phrase_pattern(phrase), text, re.IGNORECASE):
            return f"'{phrase}' in the title: not tech"
    return None

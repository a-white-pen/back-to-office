"""The matching document for one standardized row, and what is derived from it.

ONE deterministic recipe, pinned down to the library: `datasketch==2.0.0`,
default seed. Any intentional change here alters every stored fingerprint and
signature, so it is an explicit redesign and revalidation exercise with a
wholesale rebuild, never a silent edit.

This module also owns the shared title, advertiser and career-stage helpers,
because candidate generation and pair judgment must interpret them
identically, and the one statement of what canonicalization reads from a
wording's representative observation. It builds matching inputs; it never
decides that two wordings belong together.
"""

import hashlib
import json
import re
import unicodedata
from collections import defaultdict, namedtuple

from datasketch import MinHash

NUM_PERM = 128
SHINGLE_K = 5
# The frozen candidate index consumes 42 bands of three values. The stored
# vector remains 128 values; its final two positions are intentionally unused.
CANDIDATE_LSH_PARAMS = (42, 3)
CANDIDATE_SIGNATURE_VALUES = (
    CANDIDATE_LSH_PARAMS[0] * CANDIDATE_LSH_PARAMS[1])
# MinHashLSH requires a threshold, but the candidate index pins its banding
# explicitly, so this value never affects which pairs it nominates. Acceptance
# thresholds are per-lane and live in build_canonical_mapping.
JACCARD_THRESHOLD = 0.5

_WS = re.compile(r"\s+")


def collapse_ws(text):
    """Python `re` \\s runs → one ASCII space, trimmed."""
    return _WS.sub(" ", text).strip()


_LETTER_KIND = {}


def _letter_kind(char):
    """(is_letter, is_latin_letter) for one character, cached per character."""
    kind = _LETTER_KIND.get(char)
    if kind is None:
        letter = unicodedata.category(char).startswith("L")
        kind = (letter,
                letter and unicodedata.name(char, "").startswith("LATIN "))
        _LETTER_KIND[char] = kind
    return kind


def matching_description_text(description_text):
    """The description content eligible for matching: Latin script only.

    An advert that repeats its body in a second script would otherwise look
    less like the single-script version of the same job than it really is, so
    non-Latin-script description content contributes nothing to matching. This
    is a SCRIPT rule, not language detection: Latin-script text stays eligible
    whatever language it is written in. Digits, punctuation, symbols and emoji
    are script-neutral and never cause a removal.

    A token is dropped when it holds any non-Latin letter, and a line is
    dropped when nothing with a Latin letter survives in it. `description_text`
    itself is never altered; only this matching view of it is filtered.
    """
    if description_text is None:
        return None
    # Nothing in ASCII is a non-Latin letter, so there is nothing to remove and
    # nothing to clean up after a removal: English adverts keep their exact
    # stored bytes, and their fingerprints are untouched by this rule.
    if description_text.isascii():
        return description_text or None
    lines = []
    for line in description_text.split("\n"):
        kept = [token for token in line.split()
                if not any(letter and not latin
                           for letter, latin in map(_letter_kind, token))]
        rebuilt = " ".join(kept)
        if any(latin for _, latin in map(_letter_kind, rebuilt)):
            lines.append(rebuilt)
    return "\n".join(lines) or None


def matching_document(title, description_text):
    """Skip NULL components; nothing usable → no document.

    Inputs arrive already normalized — each is None or a nonempty string; the
    builder never receives "". A document that somehow collapses to empty is
    treated as no usable document (defensive guard, not a source semantic).
    """
    if description_text is not None:
        usable = matching_description_text(description_text)
        if usable is None and description_text.strip():
            # A real description that contributes no Latin-script text must
            # not fall back to matching on the title alone: unrelated adverts
            # sharing one generic non-Latin title would then share a
            # fingerprint, and exact equality groups them with no evidence.
            return None
        description_text = usable
    parts = [p for p in (title, description_text) if p is not None]
    if not parts:
        return None
    doc = collapse_ws(" ".join(parts)).lower()
    return doc or None


def fingerprint(doc):
    """Lowercase-hex SHA-256 of the UTF-8 document. Never sha256("")."""
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()


def shingles(doc):
    """The five-word shingle SET; a 1–5-word document is one shingle."""
    words = doc.split(" ")
    if len(words) <= SHINGLE_K:
        return frozenset((doc,))
    return frozenset(" ".join(words[i:i + SHINGLE_K])
                     for i in range(len(words) - SHINGLE_K + 1))


def shingle_ids(doc):
    """Each shingle → BLAKE2b digest_size=8 over UTF-8, big-endian int."""
    return frozenset(
        int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=8).digest(),
                       "big")
        for s in shingles(doc))


def minhash_signature(ids):
    """128-permutation default-seed MinHash over the shingle ids.

    Returns the 128 signature values as plain ints (unsigned 32-bit range,
    stored as ARRAY<BIGINT>). Never called with an empty id set — the pair is
    NULL when no document exists, and a sentinel signature is never produced.
    """
    m = MinHash(num_perm=NUM_PERM)
    m.update_batch([i.to_bytes(8, "big") for i in ids])
    return [int(v) for v in m.hashvalues]


def minhash_from_signature(values):
    """Rebuild a MinHash from stored signature values, for LSH insertion.

    The length check is a stored-data validity check, not part of the recipe:
    it makes a malformed stored signature say so here, where the LSH route is
    built, rather than as a numpy broadcast error."""
    if values is None or len(values) != NUM_PERM:
        raise ValueError(
            f"stored signature length "
            f"{None if values is None else len(values)} != {NUM_PERM}")
    m = MinHash(num_perm=NUM_PERM)
    m.hashvalues[:] = values
    return m


def matching_features(title, description_text):
    """The all-or-nothing matching pair for one standardized observation."""
    doc = matching_document(title, description_text)
    if doc is None:
        return None, None
    return fingerprint(doc), minhash_signature(shingle_ids(doc))


def exact_jaccard(ids_a, ids_b):
    """Exact five-word-shingle-set overlap — never a MinHash estimate.

    The measurement the evidence lanes judge; it decides nothing itself.
    """
    if not ids_a or not ids_b:
        return 0.0
    inter = len(ids_a & ids_b)
    union = len(ids_a) + len(ids_b) - inter
    return inter / union


# --- shared title and advertiser semantics ----------------------------------
#
# Deliberately dumb and deterministic: no entity resolution, no taxonomies, no
# language-specific logic.

_TITLE_TOKEN = re.compile(r"[^0-9a-z]+")
_TITLE_NOISE = frozenset({"senior", "snr", "sr", "junior", "jr"})


def normalized_advertiser(name):
    """Casefold + whitespace-collapse; None stays None."""
    if name is None:
        return None
    normalized = " ".join(str(name).casefold().split())
    return normalized or None


def title_tokens(title):
    """Casefolded alphanumeric tokens minus bare seniority markers.

    Bare seniority markers do not provide compatibility evidence on their own.
    """
    if title is None:
        return frozenset()
    tokens = {t for t in _TITLE_TOKEN.split(str(title).casefold()) if t}
    return frozenset(tokens - _TITLE_NOISE)


def titles_compatible(title_a, title_b):
    """The universal title gate.

    Compatible iff, over the filtered token sets, one side contains the other
    or the two share at least two tokens. Empty sets are never compatible.
    """
    a, b = title_tokens(title_a), title_tokens(title_b)
    if not a or not b:
        return False
    if a <= b or b <= a:
        return True
    return len(a & b) >= 2


def normalized_title(title):
    """Casefold + whitespace-collapse, for the same-advertiser+title route."""
    if title is None:
        return None
    normalized = " ".join(str(title).casefold().split())
    return normalized or None


def titles_disjoint(title_a, title_b):
    """Both titles have tokens and share none — the rotation signal.

    Used by the rotating-slot veto: a shared distinctive token can support a
    retitle, while zero shared tokens support a different job in a reused slot.
    Missing titles are never treated as disjoint because absence is not
    rotation evidence.
    """
    a, b = title_tokens(title_a), title_tokens(title_b)
    return bool(a) and bool(b) and not (a & b)


_TITLE_WORDS = re.compile(r"[0-9a-z]+(?:-[0-9a-z]+)*")


def abbreviation_tokens(title):
    """Acronyms mechanically derivable from the title itself — no dictionary.

    Two conservative constructions: the initials of one hyphenated word's
    parts, and the first letters of 3–5 consecutive whole words. A hyphenated
    word contributes one letter to a word sequence, never one per fragment.
    Two-letter acronyms are accepted only from a single hyphenated word, never
    from word sequences, because such pairs are too ambiguous to establish
    continuity.
    """
    if title is None:
        return frozenset()
    words = _TITLE_WORDS.findall(str(title).casefold())
    out = set()
    for word in words:
        parts = word.split("-")
        if len(parts) >= 2 and all(parts):
            out.add("".join(p[0] for p in parts))
    for k in (3, 4, 5):
        for i in range(len(words) - k + 1):
            out.add("".join(word[0] for word in words[i:i + k]))
    return frozenset(a for a in out if len(a) >= 2)


def title_abbreviation_continuity(title_a, title_b):
    """One title's derivable acronym appears as a token of the other.

    This can establish continuity despite zero ordinary token overlap. It is
    consulted only where rotation is being judged and never widens ordinary
    title compatibility.
    """
    return bool(abbreviation_tokens(title_a) & title_tokens(title_b)
                or abbreviation_tokens(title_b) & title_tokens(title_a))


# A stage distinction that can flip eligibility or triage must never be erased
# by canonicalization. Deliberately tiny — intern versus graduate only;
# ordinary seniority wording (VP/SVP, senior/junior) is NOT a stage.
_INTERN_TOKENS = frozenset({"intern", "internship"})
_GRADUATE_TOKENS = frozenset({"graduate", "grad"})


def career_stage(title):
    """"intern", "graduate", or None (unmarked, or ambiguously both)."""
    tokens = title_tokens(title)
    intern = bool(tokens & _INTERN_TOKENS)
    graduate = bool(tokens & _GRADUATE_TOKENS)
    if intern == graduate:            # neither, or both → no safe class
        return None
    return "intern" if intern else "graduate"


def career_stages_conflict(title_a, title_b):
    """True when both titles are clearly classified and the stages differ."""
    a, b = career_stage(title_a), career_stage(title_b)
    return a is not None and b is not None and a != b


# --- what canonicalization reads from a wording's representative -------------
#
# A fingerprint fixes its matching DOCUMENT, not the raw fields that produced
# it: two observations of one wording may differ in case, whitespace, the
# title/body split or non-Latin-script content and still hash identically.
# Raw equality is therefore the wrong question to ask about a representative.
# This is the right one, and it is the ONLY place the answer is derived, so
# the full-history path, the incremental detectors and the Spark job cannot
# drift apart.

class RepresentativeNotDetermined(RuntimeError):
    """Several observations claim one wording's representative and they do not
    agree on what canonicalization reads. No build is defined on such a
    wording — the full-history rebuild reads the same undetermined value — so
    this stops the run rather than falling back to it."""


RepresentativeSemantics = namedtuple("RepresentativeSemantics", (
    "document_fingerprint", "normalized_title", "title_tokens",
    "abbreviation_tokens", "career_stage", "body", "signature"))


def signature_values(signature):
    """The validated stored 128-value signature as a comparable tuple.

    Accepts the two transports it arrives in — a sequence from Spark, the JSON
    text the Statement API returns — so no caller has to remember which it
    holds. Validation remains over the complete stored vector even though the
    frozen candidate banding consumes only its prefix.
    """
    if signature is None:
        return None
    if isinstance(signature, str):
        signature = json.loads(signature)
    values = tuple(int(v) for v in signature)
    if len(values) != NUM_PERM:
        raise ValueError(
            f"stored signature length {len(values)} != {NUM_PERM}")
    return values


def representative_semantics(title, description_text, signature=None):
    """Everything canonicalization consumes from one representative
    observation, as one hashable value.

    Two representatives of the same wording are interchangeable to every
    consumer exactly when this is equal. Each field is here because a named
    consumer reads it:

    document_fingerprint  the matching document itself — shingles, exact
                          Jaccard and node identity. Comparing it against the
                          stored fingerprint is the recipe agreement check.
    normalized_title      the advertiser + title candidate bucket key.
    title_tokens          the universal title gate, the rotating-slot
                          disjointness test.
    abbreviation_tokens   the abbreviation-continuity exception.
    career_stage          the pair-level stage block and the component guard.
    body                  the identical-body exception (title excluded).
    signature             the stored MinHash positions consumed by the
                          frozen 42 x 3 LSH candidate banding. The complete
                          128-value stored vector is validated first.

    `normalized_title` already determines the three title derivations below
    it; they are listed anyway so the projection states its own completeness
    instead of resting on that argument.
    """
    document = matching_document(title, description_text)
    return RepresentativeSemantics(
        None if document is None else fingerprint(document),
        normalized_title(title),
        tuple(sorted(title_tokens(title))),
        tuple(sorted(abbreviation_tokens(title))),
        career_stage(title),
        matching_document(None, description_text),
        (None if signature is None else
         signature_values(signature)[:CANDIDATE_SIGNATURE_VALUES]))


def representative_digest(title, description_text, signature=None):
    """A stable hex digest of `representative_semantics`.

    The comparable form for an engine that must compare representatives
    without moving whole descriptions to a driver. JSON rather than `repr`,
    so the bytes hashed do not depend on anything but the values.
    """
    payload = json.dumps(list(representative_semantics(
        title, description_text, signature)), ensure_ascii=True,
        separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ambiguous_representatives(tied_rows):
    """Wordings whose maximum-tied observations do not agree on what
    canonicalization reads — the representative is then not determined by the
    data and no build, full or incremental, is well defined.

    tied_rows: (fingerprint, title, description_text, signature) for
    observations sharing their wording's maximum (started_at, run_id). The
    caller supplies only wordings whose tied rows differ in the RAW fields,
    which is a cheap superset: identical raw rows cannot differ here.

    Returns the offending fingerprints. An empty result is the invariant
    holding, never a decision that the ambiguity is harmless — the ordering
    key is deliberately left alone, because inventing a tie-break would
    silently change which wording the oracle reads.
    """
    seen = defaultdict(set)
    for fingerprint_value, title, text, signature in tied_rows:
        seen[fingerprint_value].add(
            representative_semantics(title, text, signature))
    return sorted(fp for fp, semantics in seen.items() if len(semantics) > 1)

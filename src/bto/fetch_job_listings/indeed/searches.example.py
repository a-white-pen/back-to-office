"""Example Indeed search configuration — a template, not a live plan.

Copy this file to searches.py in the same directory and replace the
example terms with your real ones. searches.py is intentionally
git-ignored: the search strategy is private local configuration, and
the collector will not import without it.

Every atomic term becomes its own provider query. High-confidence
spelling variants of the same concept can share one OR expression via
ALIASES_BY_ATOMIC_TERM; a grouped query quotes each phrase so OR
boundaries are unambiguous. `SEARCH_QUERIES` is the only name the
collector imports.
"""

from dataclasses import dataclass

ATOMIC_TERMS = (
    "example analyst", "example researcher", "example data engineer",
)


@dataclass(frozen=True)
class IndeedQuery:
    """One provider query and the atomic term it represents."""

    key: str                      # filename slug for the term's raw envelope
    atomic_terms: tuple
    aliases: tuple = ()

    @property
    def phrases(self):
        return self.atomic_terms + self.aliases

    @property
    def text(self):
        if len(self.phrases) == 1:
            return self.phrases[0]
        return "(" + " OR ".join(f'"{phrase}"' for phrase in self.phrases) + ")"


# Spelling variants of an existing concept, not new role families.
ALIASES_BY_ATOMIC_TERM = {
    "example data engineer": ("example data-engineer",),
}


def _key_for(term):
    return term.replace("/", "_").replace("-", "_").replace(" ", "_")


# Production SEARCH_QUERIES must include each market canary from markets.py as
# an atomic term so the all-zero health check runs.
SEARCH_QUERIES = tuple(
    IndeedQuery(
        key=_key_for(term),
        atomic_terms=(term,),
        aliases=tuple(ALIASES_BY_ATOMIC_TERM.get(term, ())),
    )
    for term in ATOMIC_TERMS
)

# Keep terms, key slugs and query texts unique. The real configuration
# also pins its exact counts here, so an accidental edit is loud.
assert len(SEARCH_QUERIES) == len(ATOMIC_TERMS)
assert len({q.key for q in SEARCH_QUERIES}) == len(SEARCH_QUERIES)
assert len({q.text for q in SEARCH_QUERIES}) == len(SEARCH_QUERIES)

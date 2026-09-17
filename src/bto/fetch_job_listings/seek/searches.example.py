"""Example SEEK-family search configuration — a template, not a live plan.

Copy this file to searches.py in the same directory and replace the
example terms with your real ones. searches.py is intentionally
git-ignored: the search strategy is private local configuration, and
the collector will not import without it.

SEEK-family search covers titles and descriptions, so terms may include
role titles, skills and tool names. Group terms however reads best;
`terms_for` is the only name the collector imports.
"""

EXAMPLE_TITLES = [
    "example analyst", "example researcher",
]

EXAMPLE_SKILLS = [
    "example data platform",
]

ACTIVE_TERMS = EXAMPLE_TITLES + EXAMPLE_SKILLS

# Keep the plan free of duplicates. The real configuration also pins its
# exact term count here, so an accidental edit is loud:
#   assert len(ACTIVE_TERMS) == <your count>
assert len(ACTIVE_TERMS) == len(set(ACTIVE_TERMS))


# Production terms_for(...) must include the applicable board/market canaries
# defined in markets.py so zero-result health checks run.
def terms_for(board, market):
    """The sweep list for one SEEK-family board × market. One shared list
    here; this function is where a market would diverge."""
    return list(ACTIVE_TERMS)

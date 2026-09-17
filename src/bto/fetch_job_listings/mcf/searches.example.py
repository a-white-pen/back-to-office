"""Example MCF search configuration — a template, not a live plan.

Copy this file to searches.py in the same directory and replace the
example terms with your real ones. searches.py is intentionally
git-ignored: the search strategy is private local configuration, and
the collector will not import without it.

MCF free-text search matches job titles, so terms are usually role
titles. Group terms however reads best; `ACTIVE_TERMS` is the only
name the collector imports.
"""

EXAMPLE_TITLES = [
    "example analyst", "example researcher", "example data engineer",
]

# Production ACTIVE_TERMS must include the canary terms defined in fetch.py so
# zero-result health checks run.
ACTIVE_TERMS = EXAMPLE_TITLES

# Keep the plan free of duplicates. The real configuration also pins its
# exact term count here, so an accidental edit is loud:
#   assert len(ACTIVE_TERMS) == <your count>
assert len(ACTIVE_TERMS) == len(set(ACTIVE_TERMS))

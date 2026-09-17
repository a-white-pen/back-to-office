"""Example LinkedIn search configuration — a template, not a live plan.

Copy this file to searches.py in the same directory and replace the
example terms with your real ones. searches.py is intentionally
git-ignored: the search strategy is private local configuration, and
the collector will not import without it.

Every term is a plain keyword string — no Boolean, OR or alias syntax.
One Actor run per market receives the whole list at once and the
provider deduplicates across it, so the list order is the actor input
order. `ACTIVE_TERMS` is the only name the collector imports.
"""

EXAMPLE_TERMS = (
    "example analyst", "example researcher", "example data engineer",
)

ACTIVE_TERMS = EXAMPLE_TERMS

# Keep the plan free of duplicates. The real configuration also pins its
# exact term count here, so an accidental edit is loud:
#   assert len(ACTIVE_TERMS) == <your count>
assert len(ACTIVE_TERMS) == len(set(ACTIVE_TERMS))

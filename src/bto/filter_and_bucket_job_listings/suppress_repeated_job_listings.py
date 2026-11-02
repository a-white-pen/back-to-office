"""Find repeat listings whose earlier copies have the same sorting result.

Compare judged listings in the same canonical job group, with matching titles
and results, over the previous 30 collection nights. Return links to qualifying
earlier listings without changing their buckets."""

from bisect import bisect_left
from collections import defaultdict
from datetime import timedelta
from itertools import islice

from . import judged, night


def title_key(title):
    return " ".join(title.split()).casefold() if title else None


def listing_identity(row):
    return row["board"], row["market"], row["board_job_id"]


def result_key(row):
    lacks = row.get("lacks")
    return (
        row.get("bucket"),
        row.get("reason"),
        None if lacks is None else tuple(sorted({v.casefold() for v in lacks})),
    )


def repeat_decisions(rows):
    """Link each repeat to the earliest eligible different listing in its 30-night window."""
    candidates = defaultdict(list)
    keyed_rows = []
    for row in rows:
        key = None
        if judged(row) and row.get("canonical_job_id") is not None:
            title = title_key(row.get("title"))
            if title:
                key = row["canonical_job_id"], title, result_key(row)
                candidates[key].append(row)
        keyed_rows.append((row, key))
    for group in candidates.values():
        group.sort(
            key=lambda other: (
                other["listing_first_seen_at"],
                *listing_identity(other),
                other["wording_first_seen_at"],
                other["id"],
            )
        )
    # First-seen order is also collection-night order. Search only the permitted
    # 30-night interval, then stop at its earliest qualifying different listing.
    first_nights = {
        key: [night(row["listing_first_seen_at"]) for row in group]
        for key, group in candidates.items()
    }
    changes = []
    for row, key in keyed_rows:
        target = None
        if key is not None:
            anchor = night(row["wording_first_seen_at"])
            nights = first_nights[key]
            start = bisect_left(nights, anchor - timedelta(days=30))
            end = bisect_left(nights, anchor)
            identity = listing_identity(row)
            for other in islice(candidates[key], start, end):
                if listing_identity(other) == identity:
                    continue
                # A changed wording of an old listing cannot suppress backwards.
                if other["wording_first_seen_at"] >= row["wording_first_seen_at"]:
                    continue
                target = other["id"]
                break
        if target != row.get("repeat_of_id"):
            changes.append({"id": row["id"], "repeat_of_id": target})
    return changes

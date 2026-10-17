"""Compute repeat pointers from judged results, with a strictly earlier target."""

from collections import defaultdict
from datetime import timedelta

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
    candidates = defaultdict(list)
    for row in rows:
        if judged(row) and row.get("canonical_job_id") is not None and title_key(row.get("title")):
            candidates[(row["canonical_job_id"], title_key(row["title"]), result_key(row))].append(
                row
            )
    changes = []
    for row in rows:
        target = None
        if judged(row) and row.get("canonical_job_id") is not None and title_key(row.get("title")):
            anchor = night(row["wording_first_seen_at"])
            qualifying = []
            for other in candidates[
                (row["canonical_job_id"], title_key(row["title"]), result_key(row))
            ]:
                if listing_identity(other) == listing_identity(row):
                    continue
                if (
                    not anchor - timedelta(days=30)
                    <= night(other["listing_first_seen_at"])
                    < anchor
                ):
                    continue
                # A changed wording of an old listing cannot suppress backwards.
                if other["wording_first_seen_at"] >= row["wording_first_seen_at"]:
                    continue
                qualifying.append(other)
            if qualifying:
                target = min(
                    qualifying,
                    key=lambda o: (
                        o["listing_first_seen_at"],
                        *listing_identity(o),
                        o["wording_first_seen_at"],
                        o["id"],
                    ),
                )["id"]
        if target != row.get("repeat_of_id"):
            changes.append({"id": row["id"], "repeat_of_id": target})
    return changes

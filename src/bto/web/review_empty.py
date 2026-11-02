"""Offer exact recovery counts from the existing metadata snapshot for an empty queue."""

from bto.web import review_cache
from bto.web.review_job_listings import review_filter_state


def alternatives(parameters, snapshot, states):
    """Count jobs recovered by removing each filter the snapshot can evaluate exactly."""
    if snapshot is None or states is None:
        return []
    published = {
        "latest_night": snapshot.latest,
        "older_nights": snapshot.older,
        "available_markets": snapshot.available_markets,
        "board_pairs": snapshot.board_pairs,
    }
    result = []
    for chip in review_filter_state(parameters, **published)["chips"]:
        if not chip["removable"]:
            continue
        label, removed = chip["label"], chip["changes"]
        args = parameters.copy()
        for key, value in removed.items():
            if value is None:
                args.pop(key, None)
            else:
                args[key] = value
        args.pop("id", None)
        args.pop("table_keep", None)
        args["page"] = "1"
        controls = review_filter_state(args, **published)
        if not review_cache.cacheable(args, controls, snapshot):
            continue
        _, count, _ = review_cache.queue(args, controls, snapshot, states)
        if count:
            result.append(
                {
                    "label": label,
                    "count": count,
                    "changes": {**removed, "id": None, "page": 1, "table_keep": None},
                }
            )
    return result

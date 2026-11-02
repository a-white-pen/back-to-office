"""Filtering and bucketing of cleaned job-listing wordings."""

from datetime import timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

SINGAPORE = ZoneInfo("Asia/Singapore")


@lru_cache(maxsize=1)
def processing_start():
    from .config import processing_start as configured_start

    return configured_start()


def __getattr__(name):
    if name == "PROCESSING_START":
        return processing_start()
    raise AttributeError(name)


HISTORY_REASON = "not read: before processing window"
# Inputs Gemini still refuses after a retry wait for a later night; more than
# the limit in one stage stop the night instead.
REFUSED_REASON = "not read: Google refused"
REFUSED_INPUT_LIMIT = 10
# Keep earlier stored reasons recognisable when reprocessing unfinished work.
DEFERRED_REASONS = (
    "not read by 8:30 AM",
    "not read: spend ceiling reached",
    REFUSED_REASON,
    "not read: the night stopped early",
)
TERMINAL_REASONS = ("triage failed twice", "extraction failed twice")
BUCKETS = (
    "top_picks",
    "in_lane",
    "less_interested",
    "not_sorted",
    "tech_not_for_me",
    "non_tech",
    "title_filter",
    "ineligible",
)
BUCKET_LABELS = dict(
    zip(
        BUCKETS,
        (
            "Top picks",
            "In lane",
            "Less interested",
            "Not sorted",
            "Tech, not for me",
            "Non-tech",
            "Title filter",
            "Ineligible",
        ),
    )
)


def night(timestamp):
    return (timestamp.astimezone(SINGAPORE) - timedelta(hours=3)).date()


def judged(row):
    return row["wording_first_seen_at"] >= processing_start() and (
        row.get("bucket") is not None and row.get("reason") not in DEFERRED_REASONS
    )


def spending_refusal(error):
    """A Gemini error saying the spending limit was reached."""
    return "spend" in str((error or {}).get("message", "")).casefold()

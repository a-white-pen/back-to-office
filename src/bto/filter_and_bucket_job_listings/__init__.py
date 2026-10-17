"""Filtering and bucketing of cleaned job-listing wordings."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SINGAPORE = ZoneInfo("Asia/Singapore")
PROCESSING_START = datetime(2026, 10, 8, 19, tzinfo=timezone.utc)
HISTORY_REASON = "not read: before processing window"
DEFERRED_REASONS = (
    "not read by 8:30 AM",
    "not read: spend ceiling reached",
    "not read: Google refused",
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


def eligible(row):
    return row["wording_first_seen_at"] >= PROCESSING_START and (
        row.get("bucket") is None or row.get("reason") in DEFERRED_REASONS
    )


def judged(row):
    return row["wording_first_seen_at"] >= PROCESSING_START and (
        row.get("bucket") is not None and row.get("reason") not in DEFERRED_REASONS
    )

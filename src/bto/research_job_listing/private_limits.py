"""Read the owner's direct-link request allowance from ignored private settings."""

import json

from bto import settings


def daily_limit():
    path = settings.private_dir() / "research_job_listing" / "limits.json"
    try:
        value = json.loads(path.read_text()).get("daily_searches")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ValueError("Private direct-link limits are missing or invalid") from error
    if type(value) is not int or value <= 0:
        raise ValueError("Private direct-link allowance must be a positive integer")
    return value

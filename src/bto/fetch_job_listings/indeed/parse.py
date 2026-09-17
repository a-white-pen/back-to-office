"""Validate Indeed Actor rows and map them into Bronze indeed_job_listings.

Called by:
    fetch_job_listings.run

An Indeed actor row already contains the full JD, so the row itself is the
payload: `content_hash` is SHA-256 over the row's canonical serialization
(the data dictionary's recipe — volatile keys such as trackingKey are
INCLUDED), and the same canonical bytes are what raw_job_listings stores.

Main functions:
    validate_row(row, config)   raises ValueError on shape drift,
                                MarketMismatchError on an out-of-market row
    canonical_bytes(row)        the exact bytes content_hash is taken over
    observation_row(...)        {column: value} for the Bronze table
"""

import json
import re

BOARD = "indeed"

JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,32}")

# Actor row keys that become Bronze columns, verbatim (see data dictionary).
PAYLOAD_COLUMNS = (
    "id", "title", "jobDescription", "jobDescriptionHTML", "salary",
    "location", "jobLocationCity", "formattedLocation", "companyDetails",
    "companyOverviewLink", "attributes", "occupations", "benefits",
    "socialInsurance", "jobTypes", "pubDate", "expirationDate",
    "jobSourceName", "originalApplyUrl", "viewJobLink", "language",
    "trackingKey", "expired", "isRepost", "newJob", "urgentlyHiring",
    "highVolumeHiring",
)


class MarketMismatchError(ValueError):
    """The provider row is not labelled as the requested market."""


def _first_text(value, names):
    """Depth-first search for the first non-empty value under any of `names` —
    the Actor has moved location fields between levels before."""
    if isinstance(value, dict):
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, (str, int)) and str(candidate).strip():
                return str(candidate).strip()
        for child in value.values():
            found = _first_text(child, names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_text(child, names)
            if found:
                return found
    return ""


def validate_row(row, config):
    """Prove one Actor row still has the fields collection depends on and
    belongs to the requested market. Returns the row's job id."""
    if not isinstance(row, dict):
        raise ValueError("Indeed Actor row is not an object")
    job_id = str(row.get("id") or "").strip()
    title = row.get("title")
    description = row.get("jobDescription")
    publication = row.get("pubDate")
    if (not JOB_ID_PATTERN.fullmatch(job_id)
            or not isinstance(title, str) or not title.strip()
            or not isinstance(description, str) or not description.strip()
            or not isinstance(publication, (str, int))):
        raise ValueError(f"Indeed Actor row shape changed: id={job_id!r}")

    location = _first_text(
        row, ("formattedLocation", "locationName", "location")).lower()
    country = _first_text(
        row, ("countryCode", "countryName", "country")).lower()
    codes = {config.country_code.lower(), config.indeed_country.lower()}
    if config.country_code == "GB":
        codes.add("uk")
    in_market = (country in codes
                 or any(marker in location for marker in config.location_markers))
    if not in_market:
        raise MarketMismatchError(
            f"Indeed row {job_id} is outside {config.country_name}: "
            f"{row.get('formattedLocation')!r}")
    return job_id


def canonical_bytes(row):
    """The exact uncompressed bytes `content_hash` is SHA-256 over, and the
    bytes stored in raw_job_listings. Dictionary recipe: the selected Actor
    row as given — volatile keys included — canonically reserialized."""
    return json.dumps(row, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def observation_row(market, board_job_id, run_id, content_hash, row):
    observation = {
        "board": BOARD,
        "market": market,
        "board_job_id": board_job_id,
        "run_id": run_id,
        "content_hash": content_hash,
    }
    for column in PAYLOAD_COLUMNS:
        observation[column] = row.get(column)
    return observation

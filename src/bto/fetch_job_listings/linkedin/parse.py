"""Validate LinkedIn Actor rows and map them into Bronze observations.

Each row carries its full JD and is preserved as the raw payload. Canonical
serialization supplies both the stored bytes and content hash. Country checks
use the job URL's country subdomain, then location text when the URL has no
country signal. Empty source strings remain empty strings in Bronze.
"""

import json
import re
import urllib.parse

BOARD = "linkedin"

JOB_ID_PATTERN = re.compile(r"[0-9]{6,24}")

# LinkedIn serves public job pages on the country's own subdomain — a
# two-letter label such as sg/hk/th/au/nz/ca (and uk, LinkedIn's spelling for
# Britain). Generic hosts (www.linkedin.com, linkedin.com) carry no country.
# Every market slug this collector supports equals its LinkedIn subdomain.
LINKEDIN_HOST_SUFFIX = ".linkedin.com"
COUNTRY_SUBDOMAIN_PATTERN = re.compile(r"[a-z]{2}")

# Actor row keys that become Bronze columns, verbatim (see data dictionary).
PAYLOAD_COLUMNS = (
    "jobId", "jobTitle", "jobDescription", "location", "sector",
    "contractType", "workType", "experienceLevel", "yearsOfExperience",
    "salaryInfo", "applicationsCount", "posterFullName", "posterProfileUrl",
    "publishedAt", "postedTime", "jobUrl", "applyUrl", "applyType",
    "searchString", "dynamicFilterMatch", "companyId", "companyName",
    "companyUrl", "companyLogo", "companyWebsite", "companyDescription",
    "companyIndustry", "companyEmployeeCount", "companyEmployeeCountRange",
    "companyOrganizationType", "companyFoundedDate", "companyFollowersCount",
    "companySpecialties", "companyAffiliatedPages", "companyOfficeLocations",
    "companyAddress", "companyRecentPosts",
)


class MarketMismatchError(ValueError):
    """The provider row is not labelled as the requested market."""


def country_subdomain(job_url):
    """The LinkedIn country subdomain of a job's public URL, or None when the
    URL carries no usable country signal.

    The hostname is parsed properly — never substring-matched over the whole
    URL, so a country code smuggled into a path or query cannot spoof it.
    None (missing, malformed, a non-LinkedIn host, a generic host such as
    www.linkedin.com, or a non-two-letter subdomain) means "no signal": the
    caller falls back to location-text validation.
    """
    if not isinstance(job_url, str) or not job_url.strip():
        return None
    try:
        hostname = urllib.parse.urlsplit(job_url.strip()).hostname or ""
    except ValueError:
        return None
    hostname = hostname.lower()
    if not hostname.endswith(LINKEDIN_HOST_SUFFIX):
        return None
    subdomain = hostname[:-len(LINKEDIN_HOST_SUFFIX)]
    if COUNTRY_SUBDOMAIN_PATTERN.fullmatch(subdomain):
        return subdomain
    return None


def validate_row(row, config):
    """Prove one Actor row still has the fields collection depends on and
    belongs to the requested market. Returns the row's job id.

    Market rule: the jobUrl country subdomain is the primary signal — equal
    to the requested market accepts the row, a different country rejects it.
    Only when the URL yields no country signal does the location-text token
    test decide, exactly as it always has.
    """
    if not isinstance(row, dict):
        raise ValueError("LinkedIn Actor row is not an object")
    job_id = str(row.get("jobId") or "").strip()
    title = row.get("jobTitle")
    location = row.get("location")
    published = row.get("publishedAt")
    description = row.get("jobDescription")
    if (not JOB_ID_PATTERN.fullmatch(job_id)
            or not isinstance(title, str) or not title.strip()
            or not isinstance(location, str) or not location.strip()
            or not isinstance(published, str) or not published.strip()
            or not isinstance(description, str) or not description.strip()):
        raise ValueError(f"LinkedIn Actor row shape changed: jobId={job_id!r}")

    subdomain = country_subdomain(row.get("jobUrl"))
    if subdomain is not None:
        # Every supported market slug equals its LinkedIn subdomain, so this
        # accepts "Greater Phuket Area" on th.linkedin.com for market th and
        # rejects a ca.linkedin.com job however its location text reads.
        if subdomain != config.market:
            raise MarketMismatchError(
                f"LinkedIn row {job_id} is outside {config.location}: "
                f"jobUrl country {subdomain!r}, location {location!r}")
    elif not any(token in location.lower() for token in config.location_tokens):
        raise MarketMismatchError(
            f"LinkedIn row {job_id} is outside {config.location}: {location!r}")
    return job_id


def canonical_bytes(row):
    """The exact uncompressed bytes `content_hash` is SHA-256 over, and the
    bytes stored in raw_job_listings — the Actor row as given, canonically
    reserialized (the data dictionary's recipe)."""
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

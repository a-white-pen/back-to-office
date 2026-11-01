"""Find employer staff on LinkedIn and read full profiles for key people.

Search recruiters and team members in the market, then wider recruiters and
leaders where needed. Search everyone locally only when those searches find
nobody. A company id must match each current position; name-only matches
remain provider-inferred. Full profiles can complete hidden surnames."""

import logging

from ..providers import apify
from .classify_found_persons import (
    RECRUITING_TITLE,
    SYNONYMS,
    FoundPerson,
    complete_half_name,
    is_half_name,
    linkedin_key,
    shared_words,
)

log = logging.getLogger(__name__)

RECRUITING_TITLES = ["Talent Acquisition", "Recruiter", "Recruitment", "Talent Partner", "Sourcer"]
# The Actor's own option names; the prices are part of the names.
SHORT_MODE = "Short ($4 per 1k)"
FULL_PROFILE_MODE = "Profile details no email ($4 per 1k)"
RECRUITER_ROWS = 12
TEAM_ROWS = 20
LEADER_ROWS = 10
EVERYONE_ROWS = 25
# Short mode costs USD 0.02 a run and 0.003 a profile, and the Actor refuses
# a maxTotalChargeUsd below USD 0.05. A full profile costs USD 0.004.
SHORT_RUN_USD = 0.02
SHORT_PROFILE_USD = 0.003
COMPANY_EMPLOYEES_MINIMUM_USD = 0.05
FULL_PROFILE_USD = 0.004


def find_people_on_linkedin(company, job_words, market_locations, runs):
    """Everyone the LinkedIn searches find at the company, as FoundPerson
    records, recruiters first. Raises apify.ApifyRefused when Apify fails."""
    place = market_locations[0] if market_locations else ""
    recruiters = _search(company, RECRUITING_TITLES, market_locations, RECRUITER_ROWS, runs, place)
    if sum(1 for p in recruiters if RECRUITING_TITLE.search(p.title or "")) < 2:
        anywhere = _search(company, RECRUITING_TITLES, [], RECRUITER_ROWS, runs, place)
        recruiters += [p for p in anywhere
                       if RECRUITING_TITLE.search(p.title or "") and shared_words(p.title, job_words)]

    single = [w for w in job_words[1:] if " " not in w]
    leaders = [f"{lead} {word}" for word in single[:2] for lead in ("Head of", "Director")]
    synonyms = [s.upper() if len(s) <= 2 else s.title()
                for w in single for s in SYNONYMS.get(w.lower(), ())]
    team_titles = list(dict.fromkeys(job_words + synonyms + leaders))
    team = []
    if team_titles:
        team = _search(company, team_titles, market_locations, TEAM_ROWS, runs, place)
    if leaders:
        team += _search(company, leaders, [], LEADER_ROWS, runs, place)
    if not recruiters and not team:
        return _search(company, [], market_locations, EVERYONE_ROWS, runs, place)
    return recruiters + team


def read_full_profiles(people, runs):
    """Read the full profiles of `people` in one run, to replace each internal
    profile link with the person's public one and to complete a half name
    from it. Changes the records in place; a profile that does not come back
    leaves its person as found."""
    wanted = {p.linkedin_url: p for p in people
              if p.linkedin_url and (linkedin_key(p.linkedin_url) is None or is_half_name(p.name))}
    if not wanted:
        return
    rows = runs.run(
        apify.PROFILE_SCRAPER,
        {"profileScraperMode": FULL_PROFILE_MODE, "urls": list(wanted)},
        # The Actor charges per profile only. The cap allows one profile more
        # than asked for, so it is never a fraction too small for the last one.
        max_charge_usd=round(FULL_PROFILE_USD * (len(wanted) + 1), 3),
    ) or []
    for row in rows:
        # Each row repeats the address it was asked for; its own id differs.
        person = wanted.get((row.get("originalQuery") or {}).get("url"))
        if person is None:
            continue
        if row.get("linkedinUrl"):
            person.linkedin_url = row["linkedinUrl"]
        person.name = complete_half_name(person.name, row.get("publicIdentifier"))


def _search(company, titles, locations, rows, runs, place):
    """Run one budgeted company search and keep matching current employees."""
    actor_input = {
        "companies": [_company_reference(company)],
        "profileScraperMode": SHORT_MODE,
        "maxItems": rows,
    }
    if titles:
        actor_input["jobTitles"] = titles
    if locations:
        actor_input["locations"] = locations
    found = runs.run(
        apify.COMPANY_EMPLOYEES, actor_input,
        max_charge_usd=max(COMPANY_EMPLOYEES_MINIMUM_USD,
                           round(SHORT_RUN_USD + SHORT_PROFILE_USD * rows, 3)),
        minimum_charge_usd=COMPANY_EMPLOYEES_MINIMUM_USD,
        max_items=rows,
    )
    people = [person for row in found or []
              if (person := _person_from_row(row, company, place)) is not None]
    log.info("LinkedIn search company=%r titles=%d in_market_only=%s found=%d",
             company.name, len(titles), bool(locations), len(people))
    return people


def _company_reference(company):
    if company.linkedin_company_id:
        return f"https://www.linkedin.com/company/{company.linkedin_company_id}"
    return company.name


def _person_from_row(row, company, place):
    """One FoundPerson from a Short-mode row, or None for a namesake company's
    employee or a row without a name."""
    name = " ".join(part for part in (row.get("firstName"), row.get("lastName")) if part)
    name = " ".join((name or row.get("name") or "").split())
    if not name:
        return None
    positions = [p for p in row.get("currentPositions") or [] if isinstance(p, dict)]
    if company.linkedin_company_id:
        positions = [p for p in positions
                     if str(p.get("companyId") or "") == str(company.linkedin_company_id)]
        if not positions:
            return None
    location = row.get("location")
    if isinstance(location, dict):
        location = location.get("linkedinText")
    location = (location or "").strip() or None
    return FoundPerson(
        name=name,
        found_by="linkedin",
        provenance="provider_observed" if company.linkedin_company_id else "provider_inferred",
        # Short mode carries the job title only inside the current positions.
        title=((positions[0].get("title") if positions else None) or None),
        location=location,
        linkedin_url=row.get("linkedinUrl") or None,
        in_market=bool(place and location and place.lower() in location.lower()),
    )

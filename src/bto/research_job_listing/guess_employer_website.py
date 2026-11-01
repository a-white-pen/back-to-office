"""Propose employer websites and their usual careers paths using free HTTP.

Use the listing's website first. Guessed domains combine the employer's name
with the market's common endings and must serve a page naming the employer;
a short name alone can point to another company.
"""

import re

from .check_employer_board import acronyms, company_name_words, names_employer
from .fetch_page import fetch, host
from .hiring_systems import is_reposting_address

# The domain endings an employer in each market most often uses, best first.
DOMAIN_ENDINGS = {"sg": (".com.sg", ".sg", ".com"), "hk": (".com.hk", ".hk", ".com"),
                  "th": (".co.th", ".th", ".com"), "au": (".com.au", ".au", ".com"),
                  "nz": (".co.nz", ".nz", ".com")}
MAX_GUESSES = 6

# Where employers usually put their careers pages, most common first.
USUAL_CAREERS_PATHS = ("/careers", "/jobs", "/career", "/en/careers", "/careers/jobs",
                       "/job-search", "/about/careers", "/join-us", "/opportunities", "/vacancies")


def _name_slugs(employer):
    """Up to three domain names to try: the name's words run together, its
    first word, and its initials."""
    words = [w for w in company_name_words(employer) if len(w) >= 3]
    slugs = []
    if words:
        slugs.append("".join(words))
        slugs.append(words[0])
    slugs += sorted(acronyms(employer))
    seen, unique = set(), []
    for slug in slugs:
        slug = re.sub(r"[^a-z0-9]", "", slug.lower())
        if slug and 2 < len(slug) <= 24 and slug not in seen:
            seen.add(slug)
            unique.append(slug)
    return unique[:3]


def employer_websites(listing, calls):
    """Websites this employer plausibly owns, best first.

    The listing's own company website is trusted without a request. A guessed
    website is trusted only when the page it returns names the employer.
    """
    websites = []
    if listing.company_website:
        website = (listing.company_website if listing.company_website.startswith("http")
                   else "https://" + listing.company_website)
        if host(website) and not is_reposting_address(website):
            websites.append(f"https://{host(website)}")
    employer = listing.employer
    if not employer:
        return websites
    tried = 0
    for slug in _name_slugs(employer):
        for ending in DOMAIN_ENDINGS.get(listing.market, (".com",)):
            if tried >= MAX_GUESSES:
                return websites
            guess = f"https://www.{slug}{ending}"
            if any(host(guess) == host(known) for known in websites):
                continue
            tried += 1
            calls["http"] = calls.get("http", 0) + 1
            page = fetch(guess, timeout=12)
            if page.status == 200 and names_employer(f"{page.title} {page.text[:1500]}", employer):
                websites.append(f"https://{host(page.final_url or guess)}")
                break
    return websites


def careers_addresses(website, how_many=None):
    """The usual careers addresses on a website's host."""
    base = website if website.startswith("http") else "https://" + website
    website_host = host(base)
    if not website_host:
        return []
    return [f"https://{website_host}{path}" for path in USUAL_CAREERS_PATHS[:how_many]]

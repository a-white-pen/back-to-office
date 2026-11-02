"""Identify the employer, its website and its recruitment/public-sector flags.

Prefer LinkedIn company details on the listing or its same-market copy.
Without a copy, an earlier LinkedIn listing can supply the company's id;
one Brave search can supply a missing website. Reuse filtering's private
employer classification rather than copying its lists here. find_emails
separately checks whether a website's domain carries known email addresses.
"""

import logging
import re
import urllib.parse
from dataclasses import dataclass

from ..filter_and_bucket_job_listings import config as sorting_config
from ..filter_and_bucket_job_listings.bucket_job_listings import employer_flags, named_employer
from ..log_errors import log_failure
from ..research_job_listing import brave_search
from ..research_job_listing.hiring_systems import is_aggregator, vendor_of
from ..storage.postgres import read
from . import config
from .find_relevant_personnel.classify_found_persons import plain_letters

log = logging.getLogger(__name__)

# Legal-entity endings that LinkedIn company names leave out.
LEGAL_ENDING = re.compile(
    r"[\s,.]*\b(pte|pty|private|ltd|limited|llc|inc|corp|co|company|sdn bhd|bhd|plc|gmbh|"
    r"sa|ag|bv|nv)\b\.?$", re.IGNORECASE)
# Advertiser names that hide the employer: there is no company to search for.
HIDDEN_EMPLOYERS = {"private advertiser", "confidential", "company confidential", "undisclosed"}
# Second-level endings under which a registrable domain has three labels.
SECOND_LEVEL_ENDINGS = ("com.sg", "edu.sg", "gov.sg", "org.sg", "com.hk", "co.th", "co.uk",
                        "com.au", "co.nz", "co.jp")
# Hosts that publish other companies' jobs or profiles, never the company's own.
NOT_COMPANY_HOSTS = ("linkedin.", "indeed.", "jobstreet.", "jobsdb.", "mycareersfuture.",
                     "glassdoor.", "seek.", "facebook.", "instagram.", "x.com", "twitter.",
                     "wikipedia.", "crunchbase.", "bloomberg.", ".jobs")


@dataclass
class Company:
    """The company a listing is for. `found_by` names the step that named it:
    linkedin_company_id, linkedin_copy_name, earlier_linkedin_listing or
    listing_name. `linkedin_copy` is this job's own LinkedIn listing, never an
    earlier listing by the same company, because its poster is shown as the
    person who posted this job."""

    name: str | None
    found_by: str | None
    linkedin_company_id: str | None
    website: str | None
    website_source: str | None
    linkedin_copy: dict | None
    is_recruitment_firm: bool | None
    is_public_sector: bool | None


def identify_company(conn, listing):
    """Name the company a listing is for, and say whether it is a recruitment firm or public sector.

    `listing` is a row from read.listing_for_research with the key's `board`
    added. Makes at most one paid web search, for the company's website, and
    never for a recruitment firm.
    """
    listing_name = without_legal_ending(
        listing.get("hiring_company_name") or listing.get("advertiser_name"))
    if listing_name and listing_name.casefold() in HIDDEN_EMPLOYERS:
        listing_name = None
    copy = linkedin_copy(listing)
    earlier = None
    if copy is None and listing_name:
        earlier = _earlier_linkedin_listing(conn, listing_name)
    linkedin = copy or earlier or {}
    linkedin_facts = linkedin.get("board_facts") or {}
    company_id = linkedin_facts.get("board_company_id")
    if earlier:
        name, found_by = earlier["advertiser_name"], "earlier_linkedin_listing"
    elif company_id:
        name, found_by = (copy.get("advertiser_name") or listing_name), "linkedin_company_id"
    elif copy and copy.get("advertiser_name"):
        name, found_by = copy["advertiser_name"], "linkedin_copy_name"
    else:
        name, found_by = listing_name, "listing_name"
    if not name or name.casefold() in HIDDEN_EMPLOYERS:
        name, found_by = None, None

    preferences = _employer_preferences()
    firm = is_recruitment_firm(listing, preferences)
    website, website_source = linkedin_facts.get("company_website"), "linkedin_website"
    if not website:
        website, website_source = None, None
        if name and not firm:
            website = find_company_website(name, listing.get("market"))
            website_source = "web_search" if website else None
    return Company(
        name=name, found_by=found_by,
        linkedin_company_id=str(company_id) if company_id else None,
        website=website, website_source=website_source, linkedin_copy=copy,
        is_recruitment_firm=firm,
        is_public_sector=is_public_sector(listing, preferences),
    )


def _earlier_linkedin_listing(conn, company_name):
    """Apply the same employer-name rule to historical and current listings."""
    wanted = without_legal_ending(company_name)
    if not wanted:
        return None
    return next((row for row in read.linkedin_listings_with_name(conn, wanted)
                 if (without_legal_ending(row["advertiser_name"]) or "").casefold() == wanted.casefold()), None)


def linkedin_copy(listing):
    """The listing itself when it is on LinkedIn, else its same-market
    LinkedIn copy, else None."""
    if listing.get("board") == "linkedin":
        return listing
    return next((copy for copy in listing.get("same_market_copies") or []
                 if copy.get("board") == "linkedin"), None)


def without_legal_ending(name):
    """The name without legal endings, removed one at a time: "Example Pte.
    Ltd." becomes "Example"."""
    name = " ".join((name or "").split())
    while True:
        shorter = LEGAL_ENDING.sub("", name).strip(" ,.")
        if shorter == name or not shorter:
            return name or None
        name = shorter


def is_recruitment_firm(listing, preferences):
    """True, False, or None when no rule decides. First answer wins: the
    stored mark, filtering and bucketing's rule, then MCF naming a hiring
    company that is not the advertiser."""
    if listing.get("is_recruitment_firm") is not None:
        return listing["is_recruitment_firm"]
    if preferences is not None:
        # The rule's second argument is the AI reading of the JD from filtering
        # and bucketing. Without it, the rule answers only True or None.
        firm, _ = employer_flags(listing, None, preferences)
        if firm is not None:
            return firm
    hiring = (listing.get("hiring_company_name") or "").strip().casefold()
    advertiser = (listing.get("advertiser_name") or "").strip().casefold()
    # On MCF, an advertiser that names a different hiring company posts for another employer.
    if listing.get("board") == "mcf" and hiring and advertiser and hiring != advertiser:
        return True
    return None


def is_public_sector(listing, preferences):
    """True, False, or None when nothing says."""
    if listing.get("is_public_sector") is not None:
        return listing["is_public_sector"]
    if preferences is None:
        return None
    name = listing.get("hiring_company_name") or listing.get("advertiser_name")
    if named_employer(name, preferences["employers"]["public_sector_organisations"]):
        return True
    return None


def find_company_website(name, market):
    """The company's own website from one web search, or None."""
    place = config.place_name(market)
    query = f"{name} official website {place}".strip()
    # A refused search answers nothing, and the company simply has no website.
    answers = brave_search.search(query, key_name=config.BRAVE_FIND_EMPLOYER_API_KEY, count=5)
    for answer in answers:
        domain = registrable_domain(answer.get("url"))
        if is_company_domain(domain) and domain_names_company(domain, name):
            return f"https://{domain}"
    return None


def registrable_domain(url):
    """careers.example.com → example.com; a.example.com.sg → example.com.sg."""
    url = url or ""
    host = (urllib.parse.urlsplit(url if "//" in url else f"//{url}").hostname or "").lower()
    labels = host.split(".")
    keep = 3 if any(host.endswith("." + ending) for ending in SECOND_LEVEL_ENDINGS) else 2
    return ".".join(labels[-keep:]) if len(labels) >= keep else host


def is_company_domain(domain):
    """False for a hiring-software vendor, a job board or another site that
    lists many companies."""
    return bool(domain) and not vendor_of(f"https://{domain}") and not is_aggregator(
        f"https://{domain}") and not any(host in domain for host in NOT_COMPANY_HOSTS)


def domain_names_company(domain, company_name):
    """True when the domain's first label spells the company's leading words:
    example.com for "Example", examplemedia.com for "Example Media Singapore",
    never example-logistics.cn for "Example"."""
    stem = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    words = re.findall(r"[a-z0-9]+", (company_name or "").lower())
    return bool(stem) and any(stem == "".join(words[:k]) for k in range(1, len(words) + 1))


def text_names_company(text, company_name):
    """True when `text`, such as a LinkedIn headline, names the company as a
    whole word: "Data Engineer at Example" names Example, "Examples Ltd" does not."""
    wanted = re.escape(plain_letters(company_name or ""))
    return bool(wanted) and re.search(rf"(?<![a-z0-9]){wanted}(?![a-z0-9])",
                                      plain_letters(text or "")) is not None


def _employer_preferences():
    """The owner's private employer names lists, or None when this checkout has none."""
    try:
        return sorting_config.load_preferences()
    except Exception as error:
        log_failure(log, "Employer classification preferences unavailable; private rules skipped",
                    error=error, level=logging.WARNING)
        return None
